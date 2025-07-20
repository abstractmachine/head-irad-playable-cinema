import os
import cv2

from PyQt5.QtCore import Qt, pyqtSignal, QUrl, QObject, QThread
from PyQt5.QtMultimedia import QMediaContent, QMediaPlayer
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSizePolicy, QSlider, QVBoxLayout, QWidget
)

SEEK_NORMAL = "1"
SEEK_FAST = "30"
FRAMES_PER_SHOT = 30  # Assuming a default value, adjust as necessary

class PlayerWindow(QMainWindow):

    # Define signals for communication
    video_loaded = pyqtSignal(str)
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    video_timecode_changed = pyqtSignal(int)
    frames_extracted = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self._pending_save_data = {}
        self.video_title = "[No video loaded]"
        self.timecode = "00:00:00"
        self.update_window_title()
        self.setGeometry(100, 100, 900, 600)
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
        self.normal_seek.setToolTip("Normal seek speed in seconds")
        self.fast_seek.setToolTip("Fast seek speed in seconds")
        self.normal_seek.setFixedWidth(40)
        self.fast_seek.setFixedWidth(40)
        self.normal_seek.setFocusPolicy(Qt.ClickFocus)
        self.fast_seek.setFocusPolicy(Qt.ClickFocus)
        self.normal_seek.setAlignment(Qt.AlignCenter)
        self.fast_seek.setAlignment(Qt.AlignCenter)
        self.normal_seek.editingFinished.connect(self.validate_normal_seek)
        self.fast_seek.editingFinished.connect(self.validate_fast_seek)

        # Load button
        self.load_button = QPushButton("Load")
        self.load_button.setToolTip("Load video\nShortcut: L or V")
        self.load_button.clicked.connect(self.load_video)

        # Play/Pause button
        self.play_pause_button = QPushButton("Play")
        self.play_pause_button.setToolTip("Play or pause video\nShortcut:Space")
        self.play_pause_button.clicked.connect(self.toggle_play_pause)
        self.play_pause_button.setEnabled(False)
        self.is_playing = False

        # Seek buttons
        self.back_button = QPushButton("Back")
        self.back_button.setToolTip("Seek backward\nShortcut: Left arrow, Shift for fast")
        self.back_button.setEnabled(False)
        self.back_button.clicked.connect(self.seek_back)
        self.forward_button = QPushButton("Forward")
        self.forward_button.setToolTip("Seek forward\nShortcut: Right arrow, Shift for fast")
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
        button_width = 120
        for btn in [self.load_button, self.play_pause_button, self.back_button, self.forward_button]:
            btn.setFixedWidth(button_width)

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
        self.timeline.sliderReleased.connect(self.update_position_from_slider)
        
        # Connect preference signals
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "x": pos.x(),
            "y": pos.y(),
            "width": size.width(),
            "height": size.height(),
            "normal_seek": self.normal_seek.text(),
            "fast_seek": self.fast_seek.text()
        }

    def on_request_load(self, data):
        if "normal_seek" in data:
            self.normal_seek.setText(data["normal_seek"])
        if "fast_seek" in data:
            self.fast_seek.setText(data["fast_seek"])
        if "x" in data and "y" in data:
            self.move(data["x"], data["y"])
        if "width" in data and "height" in data:
            self.resize(data["width"], data["height"])

    def update_window_title(self):
        duration_seconds = getattr(self, "duration_seconds", None)
        if duration_seconds is not None and duration_seconds > 0:
            h = duration_seconds // 3600
            m = (duration_seconds % 3600) // 60
            s = duration_seconds % 60
            duration_str = f"{h:02}:{m:02}:{s:02}"
            self.setWindowTitle(f"{self.video_title} | {self.timecode} / {duration_str}")
        else:
            self.setWindowTitle(f"{self.video_title} | {self.timecode}")

    def update_duration(self, duration):
        self.timeline.setRange(0, duration)
        self.duration_seconds = duration // 1000
        self.update_window_title()
        
    def load_video(self):
        file_dialog = QFileDialog(self)
        file_path, _ = file_dialog.getOpenFileName(self, "Load Video", "", "Video Files (*.mp4 *.avi *.mov)")
        if file_path:
            self.video_title = file_path.split('/')[-1]
            self.update_window_title()
            self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(file_path)))
            # print("Media set:", file_path)
            self.play_pause_button.setEnabled(True)
            self.back_button.setEnabled(True)
            self.forward_button.setEnabled(True)
            self.timeline.setEnabled(True)
            self.media_player.play()
            self.media_player.pause()
            self.media_player.setPosition(0)  # Added line to seek to the start
            self.current_video_path = file_path
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
        seconds = position // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        self.set_timecode(f"{h:02}:{m:02}:{s:02}")
        self.emit_timecode_changed(position)

    def set_position(self, position):
        self.media_player.setPosition(position)

    def emit_timecode_changed(self, position):
        self.video_timecode_changed.emit(position)

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

    def jump_to_timecode(self, timecode, is_last_frame=False):
        parts = timecode.split(":")
        if len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            ms = int((h * 3600 + m * 60 + s) * 1000)
            if is_last_frame and self.current_video_path:
                from scenedetect import open_video
                video = open_video(self.current_video_path)
                fps = video.frame_rate  # Use frame_rate instead of get_fps()
                frame_duration = int(1000 / fps)
                ms = max(ms - frame_duration, 0)
            self.media_player.setPosition(ms)
        else:
            print(f"Invalid timecode format: {timecode}")
    
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
        elif key == Qt.Key_Space:
            if self.play_pause_button.isEnabled():
                self.toggle_play_pause()
        elif key == Qt.Key_Left:
            if self.back_button.isEnabled():
                self.seek_video(-seek_amount)
        elif key == Qt.Key_Right:
            if self.forward_button.isEnabled():
                self.seek_video(seek_amount)

    def closeEvent(self, event):
        try:
            self.media_player.stop()
        except Exception:
            pass
        super().closeEvent(event)

    def update_position_from_slider(self):
        self.media_player.setPosition(self.timeline.value())

    def extract_frames_for_timecodes(self, timecodes):
        
        #Extract frames for the given timecodes from the current video.
        #Returns a list of numpy arrays (frames).
        
        if not self.current_video_path:
            print("No video loaded for frame extraction.")
            return []

        cap = cv2.VideoCapture(self.current_video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = []

        def timecode_to_frame(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                total_seconds = h * 3600 + m * 60 + s
                return int(total_seconds * fps)
            return None

        for tc in timecodes:
            frame_num = timecode_to_frame(tc)
            if frame_num is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
                    print(f"Extracted frame {frame_num} for timecode {tc}")
                else:
                    print(f"Failed to extract frame at {tc} (frame {frame_num})")
            else:
                print(f"Invalid timecode: {tc}")

        cap.release()
        return frames

    def handle_shot_timecodes(self, start_tc, timecodes):
        # print(f"Handling shot timecodes: start={start_tc}, count={len(timecodes)}")
        all_timecodes = [start_tc] + timecodes
        self.frame_thread = QThread()
        self.frame_worker = FrameExtractorWorker(self.current_video_path, all_timecodes)
        self.frame_worker.moveToThread(self.frame_thread)
        self.frame_thread.started.connect(self.frame_worker.run)
        self.frame_worker.finished.connect(self.handle_frames_extracted)
        self.frame_worker.finished.connect(self.frame_thread.quit)
        self.frame_worker.finished.connect(self.frame_worker.deleteLater)
        self.frame_thread.finished.connect(self.frame_thread.deleteLater)
        self.frame_thread.start()

    def handle_frames_extracted(self, frames):
        # print(f"Extracted {len(frames)} frames for API.")
        self.frames_extracted.emit(frames)  # Emit frames to AnnotateWindow

class FrameExtractorWorker(QObject):
    finished = pyqtSignal(list)  # Emits list of frames

    def __init__(self, video_path, timecodes):
        super().__init__()
        self.video_path = video_path
        self.timecodes = timecodes

    def run(self):
        import cv2
        cap = cv2.VideoCapture(self.video_path)
        fps = cap.get(cv2.CAP_PROP_FPS)
        frames = []

        def timecode_to_frame(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                total_seconds = h * 3600 + m * 60 + s
                return int(total_seconds * fps)
            return None

        for tc in self.timecodes:
            frame_num = timecode_to_frame(tc)
            if frame_num is not None:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_num)
                ret, frame = cap.read()
                if ret:
                    frames.append(frame)
        cap.release()
        self.finished.emit(frames)
