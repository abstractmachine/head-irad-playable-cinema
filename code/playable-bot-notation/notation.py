import sys
import os
import cv2
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSlider, QFileDialog, QSizePolicy, QTextEdit
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap

import openai
import base64
import mimetypes

# use a file for API key to hide from version control
# local file is called API_KEY.txt

openai.api_key = os.environ.get("OPENAI_API_KEY", "your-api-key")

def timecode_from_frame(frame_idx, fps):
    total_seconds = frame_idx / fps if fps else 0
    h = int(total_seconds // 3600)
    m = int((total_seconds % 3600) // 60)
    s = int(total_seconds % 60)
    ms = int((total_seconds - int(total_seconds)) * 1000)
    return f"{h:02d}-{m:02d}-{s:02d}-{ms:03d}"

class VideoPlayer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Simple Video Player")
        self.setFocusPolicy(Qt.StrongFocus)
        self.video_path = None
        self.cap = None
        self.frame_count = 0
        self.fps = 0
        self.current_frame_idx = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.next_frame)
        self.is_playing = False

        self.bot_timer = QTimer()
        self.bot_timer.timeout.connect(self.bot_notation_step)
        self.bot_notation_active = False

        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        # Video display
        self.video_label = QLabel("Load a video to begin.")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.video_label, stretch=10)

        # Timeline slider
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self.pause)
        self.slider.sliderReleased.connect(self.slider_released)
        self.slider.valueChanged.connect(self.slider_scrub)
        layout.addWidget(self.slider)

        # Buttons
        btn_layout = QHBoxLayout()
        self.load_btn = QPushButton("Load Video")
        self.load_btn.clicked.connect(self.load_video)
        btn_layout.addWidget(self.load_btn)

        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self.toggle_play)
        self.play_btn.setEnabled(False)
        btn_layout.addWidget(self.play_btn)

        self.bot_btn = QPushButton("Bot Notation")
        self.bot_btn.clicked.connect(self.start_bot_notation)
        self.bot_btn.setEnabled(False)
        btn_layout.addWidget(self.bot_btn)

        layout.addLayout(btn_layout)

        # Caption text field
        self.caption_field = QTextEdit()
        self.caption_field.setReadOnly(True)
        self.caption_field.setPlaceholderText("Captions will appear here...")
        self.caption_field.setFixedHeight(60)
        layout.addWidget(self.caption_field)

        self.setLayout(layout)

    def load_video(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Video File", "", "Video Files (*.mp4 *.avi *.mov)")
        if path:
            self.video_path = path
            self.cap = cv2.VideoCapture(self.video_path)
            self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))
            self.fps = self.cap.get(cv2.CAP_PROP_FPS)
            self.slider.setMaximum(self.frame_count - 1)
            self.slider.setEnabled(True)
            self.play_btn.setEnabled(True)
            self.bot_btn.setEnabled(True)
            self.current_frame_idx = 0
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_idx)
            ret, frame = self.cap.read()
            if ret:
                self.display_frame(frame)
            self.slider.setValue(self.current_frame_idx)
            self.caption_field.setText("")  # Clear captions on new load

    def display_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qt_image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qt_image)
        label_w = self.video_label.width()
        label_h = self.video_label.height()
        pixmap = pixmap.scaled(label_w, label_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.video_label.setPixmap(pixmap)
        self.slider.blockSignals(True)
        self.slider.setValue(self.current_frame_idx)
        self.slider.blockSignals(False)
        # Example: Show a dummy caption for the current frame
        self.caption_field.setText(f"Caption for frame {self.current_frame_idx}")

    def show_frame(self, idx):
        if not self.cap:
            return
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = self.cap.read()
        if ret:
            self.display_frame(frame)
            self.current_frame_idx = idx

    def resizeEvent(self, event):
        if self.cap:
            # Re-display current frame on resize
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_idx)
            ret, frame = self.cap.read()
            if ret:
                self.display_frame(frame)
        super().resizeEvent(event)

    def toggle_play(self):
        if self.is_playing:
            self.pause()
        else:
            self.play()

    def play(self):
        if not self.cap:
            return
        self.is_playing = True
        self.play_btn.setText("Pause")
        self.timer.start(int(1000 / self.fps) if self.fps > 0 else 30)

    def pause(self):
        self.is_playing = False
        self.play_btn.setText("Play")
        self.timer.stop()

    def next_frame(self):
        if not self.cap:
            return
        # Use cap.read() for sequential playback (fast)
        ret, frame = self.cap.read()
        if ret:
            self.current_frame_idx += 1
            self.display_frame(frame)
        else:
            self.pause()

    def slider_released(self):
        idx = self.slider.value()
        self.show_frame(idx)

    def slider_scrub(self, idx):
        if self.slider.isSliderDown():
            self.show_frame(idx)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_V:
            self.load_btn.click()
            return
        if key in (Qt.Key_Space,):  # Spacebar for play/pause (cross-platform)
            self.toggle_play()
            return
        super().keyPressEvent(event)

    # --- Bot Notation ---
    def start_bot_notation(self):
        if not self.cap or not self.video_path:
            return
        if self.bot_notation_active:
            # Stop bot notation if already running
            self.bot_timer.stop()
            self.bot_notation_active = False
            self.bot_btn.setText("Bot Notation")
            self.bot_btn.setEnabled(True)
            return
        # Start bot notation
        self.pause()
        self.bot_btn.setText("Bot Notating")
        self.bot_btn.setEnabled(True)
        self.bot_notation_active = True
        self.bot_timer.start(1)  # Start immediately

    def bot_notation_step(self):
        if not self.cap or not self.video_path or not self.bot_notation_active:
            self.bot_timer.stop()
            self.bot_btn.setText("Bot Notation")
            self.bot_btn.setEnabled(True)
            self.bot_notation_active = False
            return

        # Save current frame as jpg and txt
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_idx)
        ret, frame = self.cap.read()
        if not ret:
            self.bot_timer.stop()
            self.bot_btn.setText("Bot Notation")
            self.bot_btn.setEnabled(True)
            self.bot_notation_active = False
            return

        # Prepare notations folder
        notations_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notations")
        os.makedirs(notations_dir, exist_ok=True)

        # Prepare filename
        base = os.path.splitext(os.path.basename(self.video_path))[0].replace(" ", "")
        tc = timecode_from_frame(self.current_frame_idx, self.fps)
        filename = f"{base}-{tc}"
        jpg_path = os.path.join(notations_dir, filename + ".jpg")
        txt_path = os.path.join(notations_dir, filename + ".txt")
        print(f"Saving frame {self.current_frame_idx} to {jpg_path} and {txt_path}")

        # Save image
        cv2.imwrite(jpg_path, frame)
        # Save dummy text (replace with real caption if needed)
        with open(txt_path, "w") as f:
            f.write(f"Caption for frame {self.current_frame_idx}")

        # Advance 0.1 seconds
        next_frame = int(self.current_frame_idx + max(1, int(self.fps * 0.1)))
        if next_frame >= self.frame_count:
            self.bot_timer.stop()
            self.bot_btn.setText("Bot Notation")
            self.bot_btn.setEnabled(True)
            self.bot_notation_active = False
            return

        self.current_frame_idx = next_frame
        self.show_frame(self.current_frame_idx)
        # Continue to next step
        self.bot_timer.start(1)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoPlayer()
    window.resize(900, 600)
    window.show()
    sys.exit(app.exec_())