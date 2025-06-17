import sys, os, cv2
import torch
from ultralytics import YOLO

from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QSlider
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap

# Load YOLO model
model = YOLO("model.pt")
device = "mps" if torch.backends.mps.is_available() else "cpu"
model.to(device)

# Initialize Qt app
app = QApplication(sys.argv)
class ResizableWindow(QWidget):
    def resizeEvent(self, event):
        w, h = self.width(), self.height()
        print(f"Window resized to: {w} x {h}")
        super().resizeEvent(event)

window = ResizableWindow()
window.setWindowTitle("Playable Cinema – Real-Time Prediction")

# Video capture
video_path = os.path.abspath("video.mp4")
cap = cv2.VideoCapture(video_path)
fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
duration_ms = int((total_frames / fps) * 1000)

# UI elements
video_label = QLabel()
video_label.setAlignment(Qt.AlignCenter)

play_button = QPushButton("Play")
pause_button = QPushButton("Pause")

slider = QSlider(Qt.Horizontal)
slider.setRange(0, duration_ms)

# Timers
play_timer = QTimer()

# Frame tracking
current_time_ms = 0
frame_buffer = None

# ---- FRAME PROCESSING ----

def show_frame(frame):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    image = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
    video_label.setPixmap(QPixmap.fromImage(image).scaled(
        video_label.width(), video_label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation
    ))

def display_frame():
    global current_time_ms, frame_buffer
    ret, frame = cap.read()
    if not ret:
        play_timer.stop()
        return

    frame_buffer = frame.copy()

    # Run YOLO prediction on every frame
    result = model.predict(frame_buffer, verbose=False)[0]
    annotated = result.plot()
    show_frame(annotated)

    current_time_ms += int(1000 / fps)
    slider.setValue(current_time_ms)

# ---- CONTROLS ----

def play():
    play_timer.start(int(1000 / fps))

def pause():
    play_timer.stop()

def seek(position_ms):
    global current_time_ms, frame_buffer
    current_time_ms = position_ms
    frame_index = int((position_ms / 1000) * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ret, frame = cap.read()
    if ret:
        frame_buffer = frame.copy()
        result = model(frame_buffer)[0]
        annotated = result.plot()
        show_frame(annotated)

# ---- SIGNALS ----

play_button.clicked.connect(play)
pause_button.clicked.connect(pause)
slider.sliderMoved.connect(seek)
play_timer.timeout.connect(display_frame)

# ---- LAYOUT ----

layout = QVBoxLayout()
layout.addWidget(video_label)
layout.addWidget(slider)
layout.addWidget(play_button)
layout.addWidget(pause_button)

window.setLayout(layout)
window.resize(921, 640)
window.show()

sys.exit(app.exec_())
