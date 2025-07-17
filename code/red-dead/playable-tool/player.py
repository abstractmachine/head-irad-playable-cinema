# Application stuff
import os
import json
from PyQt5.QtWidgets import QApplication

# PyQt5 Interface Stuff
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QSlider, QLineEdit, QFileDialog, QSizePolicy
)
from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QSizePolicy

# Video Player Stuff
from PyQt5.QtMultimedia import QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtCore import QUrl
from PyQt5.QtMultimedia import QMediaContent  # Add this import at the top

# signals from one window to another
from PyQt5.QtCore import pyqtSignal

SEEK_NORMAL = "1"
SEEK_FAST = "30"

PREFS_PATH = "preferences.json"

class PlayerWindow(QMainWindow):
    
    video_loaded = pyqtSignal(str)  # Signal with video path

    def __init__(self):
        super().__init__()
        self.video_title = "[No video loaded]"
        self.timecode = "00:00:00"
        self.update_window_title()
        self.setGeometry(100, 100, 900, 600)

        # remember for later the current video path
        self.current_video_path = None

        # Video playback setup
        self.media_player = QMediaPlayer(None, QMediaPlayer.VideoSurface)
        self.video_widget = QVideoWidget()
        self.media_player.setVideoOutput(self.video_widget)

        # Timeline slider
        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setRange(0, 100)
        self.timeline.setValue(0)
        self.timeline.setEnabled(False)
        self.timeline.setMinimumHeight(32)
        self.timeline.setToolTip("Scrub through the video timeline")

        # Seek speed controls
        self.normal_seek = QLineEdit(SEEK_NORMAL)
        self.fast_seek = QLineEdit(SEEK_FAST)
        self.normal_seek.setFixedWidth(40)
        self.fast_seek.setFixedWidth(40)
        self.normal_seek.setFocusPolicy(Qt.ClickFocus)
        self.fast_seek.setFocusPolicy(Qt.ClickFocus)
        self.normal_seek.setAlignment(Qt.AlignCenter)
        self.fast_seek.setAlignment(Qt.AlignCenter)
        self.normal_seek.editingFinished.connect(self.validate_normal_seek)
        self.fast_seek.editingFinished.connect(self.validate_fast_seek)

        # --- Load seek values from preferences.json ---
        if os.path.exists(PREFS_PATH):
            with open(PREFS_PATH, "r") as f:
                prefs = json.load(f)
            player_prefs = prefs.get("player", {})
            if "normal_seek" in player_prefs:
                self.normal_seek.setText(player_prefs["normal_seek"])
            if "fast_seek" in player_prefs:
                self.fast_seek.setText(player_prefs["fast_seek"])
        # --- end load ---

        # Load button
        self.load_button = QPushButton("Load")
        self.load_button.setToolTip("Load video (Shortcut: L or V)")
        self.load_button.clicked.connect(self.load_video)

        # Play/Pause button
        self.play_pause_button = QPushButton("Play")
        self.play_pause_button.setToolTip("Play or pause video (Space or P)")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setEnabled(False)
        self.is_playing = False

        # Seek buttons
        self.back_button = QPushButton("Back")
        self.back_button.setToolTip("Seek backward (Left arrow, Shift for fast)")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.seek_back)
        self.forward_button = QPushButton("Forward")
        self.forward_button.setToolTip("Seek forward (Right arrow, Shift for fast)")
        self.forward_button.setEnabled(False)
        self.forward_button.clicked.connect(self.seek_forward)

        # Make buttons expand equally
        for btn in [self.load_button, self.play_pause_button, self.back_button, self.forward_button]:
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        # Layouts
        seek_layout = QHBoxLayout()
        seek_layout.addWidget(QLabel("Seek:"))
        seek_layout.addWidget(self.normal_seek)
        seek_layout.addWidget(QLabel("Fast:"))
        seek_layout.addWidget(self.fast_seek)
        seek_layout.addStretch()

        # Set a fixed width for buttons
        button_width = 120  # Adjust as needed
        for btn in [self.load_button, self.play_pause_button, self.back_button, self.forward_button]:
            btn.setFixedWidth(button_width)

        # Layouts
        controls_layout = QHBoxLayout()
        controls_layout.addWidget(self.load_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.play_pause_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.back_button)
        controls_layout.addStretch()
        controls_layout.addWidget(self.forward_button)
        controls_layout.addStretch()
        controls_layout.addLayout(seek_layout)

        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.addWidget(self.video_widget)
        layout.addWidget(self.timeline)
        layout.addLayout(controls_layout)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Connect signals
        self.media_player.positionChanged.connect(self.update_position)
        self.media_player.durationChanged.connect(self.update_duration)
        self.timeline.sliderMoved.connect(self.set_position)

    def update_window_title(self):
        self.setWindowTitle(f"{self.video_title} | {self.timecode}")

    def load_video(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName(self, "Load Video", "", "Video Files (*.mp4 *.avi *.mov)")
        if file_path:
            self.video_title = file_path.split('/')[-1]
            self.update_window_title()
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
            self.play_pause_button.setEnabled(True)
            self.back_button.setEnabled(True)
            self.forward_button.setEnabled(True)
            self.timeline.setEnabled(True)
            self.media_player.play()
            self.media_player.pause()
            # remember the current video path so we can analyze it in Detector
            self.current_video_path = file_path
              # Emit signal that video has been loaded
            self.video_loaded.emit(file_path)

    def set_timecode(self, timecode):
        self.timecode = timecode
        self.update_window_title()

    def toggle_play_pause(self):
        if self.media_player.state() == QMediaPlayer.PlayingState:
            self.media_player.pause()
            self.play_pause_button.setText("Play")
            self.is_playing = False
        else:
            self.media_player.play()
            self.play_pause_button.setText("Pause")
            self.is_playing = True

    def seek_back(self):
        seek_amount = int(self.normal_seek.text() or "1")
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            seek_amount = int(self.fast_seek.text() or "10")
        self.seek_video(-seek_amount)

    def seek_forward(self):
        seek_amount = int(self.normal_seek.text() or "1")
        modifiers = QApplication.keyboardModifiers()
        if modifiers & Qt.ShiftModifier:
            seek_amount = int(self.fast_seek.text() or "10")
        self.seek_video(seek_amount)
            
    def update_position(self, position):
        self.timeline.setValue(position)
        # Update timecode in window title
        seconds = position // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.set_timecode(f"{h:02}:{m:02}:{s:02}")

    def update_duration(self, duration):
        self.timeline.setRange(0, duration)

    def set_position(self, position):
        self.media_player.setPosition(position)

    def seek_video(self, seconds):
        new_position = self.media_player.position() + (seconds * 1000)
        self.media_player.setPosition(max(0, min(new_position, self.media_player.duration())))

    def validate_normal_seek(self):
        value = self.normal_seek.text()
        if not value.isdigit() or int(value) <= 0:
            self.normal_seek.setText("1")
        self.normal_seek.clearFocus()

    def validate_fast_seek(self):
        value = self.fast_seek.text()
        if not value.isdigit() or int(value) <= 0:
            self.fast_seek.setText("10")
        self.fast_seek.clearFocus()
    
    def handle_global_key(self, event):
        focus_widget = QApplication.focusWidget()
        if not isinstance(focus_widget, QLineEdit):
            self.keyPressEvent(event)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()
        normal_seek = int(self.normal_seek.text() or "1")
        fast_seek = int(self.fast_seek.text() or "10")
        seek_amount = normal_seek

        if modifiers & Qt.ShiftModifier:
            seek_amount = fast_seek

        if key in (Qt.Key_L, Qt.Key_V):
            self.load_video()
        elif key in (Qt.Key_Space, Qt.Key_P):
            if self.play_pause_button.isEnabled():
                self.toggle_play_pause()
        elif key == Qt.Key_Left:
            if self.back_button.isEnabled():
                self.seek_video(-seek_amount)
        elif key == Qt.Key_Right:
            if self.forward_button.isEnabled():
                self.seek_video(seek_amount)