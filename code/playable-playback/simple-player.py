import sys, os
import torch, cv2
from ultralytics import YOLO

from PyQt5.QtWidgets import QApplication, QWidget, QPushButton, QVBoxLayout, QSlider
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtCore import QUrl, Qt

app = QApplication(sys.argv)

window = QWidget()
window.setWindowTitle("Playable Cinema – Timeline")

video_widget = QVideoWidget()
player = QMediaPlayer()
player.setVideoOutput(video_widget)

# Load video
video_path = os.path.abspath("video.mp4")
print("Loading video from:", video_path)
player.setMedia(QMediaContent(QUrl.fromLocalFile(video_path)))

# Playback controls
play_button = QPushButton("Play")
pause_button = QPushButton("Pause")

# Timeline slider
slider = QSlider(Qt.Horizontal)
slider.setRange(0, 0)

# Events
play_button.clicked.connect(player.play)
pause_button.clicked.connect(player.pause)

# Sync slider with video progress
def update_slider(position):
    slider.setValue(position)

def update_range(duration):
    slider.setRange(0, duration)

def seek(position):
    player.setPosition(position)

player.positionChanged.connect(update_slider)
player.durationChanged.connect(update_range)
slider.sliderMoved.connect(seek)

# Layout
layout = QVBoxLayout()
layout.addWidget(video_widget)
layout.addWidget(slider)
layout.addWidget(play_button)
layout.addWidget(pause_button)

window.setLayout(layout)
window.resize(854, 480)
window.show()

sys.exit(app.exec_())
