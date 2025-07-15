import sys
import os
import argparse
import cv2
from PIL import Image
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QSlider, QFileDialog, QTextEdit, QSizePolicy
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QPixmap, QImage

import time
import psutil

# --- Add these imports for BLIP-2 ---
from transformers import Blip2Processor, Blip2ForConditionalGeneration

# --- Argument parsing for model path ---
parser = argparse.ArgumentParser()
parser.add_argument('--model', type=str, default=None, help="Path to BLIP-2 model folder")
args, unknown = parser.parse_known_args()

class AnnotateThread(QThread):
    finished = pyqtSignal(str)

    def __init__(self, pil_img, blip2_model):
        super().__init__()
        self.pil_img = pil_img
        self.blip2_model = blip2_model

    def run(self):
        annotation = self.blip2_model.annotate(self.pil_img)
        self.finished.emit(annotation)

# --- BLIP-2 Model Loader ---
class BLIP2Annotator:
    def __init__(self, model_path):
        print(f"Loading BLIP-2 model from: {model_path}")
        process = psutil.Process(os.getpid())
        mem_before = process.memory_info().rss
        t0 = time.time()
        self.processor = Blip2Processor.from_pretrained(model_path)
        self.model = Blip2ForConditionalGeneration.from_pretrained(model_path, device_map="auto")
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)
        t1 = time.time()
        mem_after = process.memory_info().rss
        self.model_mem_mb = (mem_after - mem_before) / (1024 * 1024)
        self.load_time = t1 - t0
        print(f"Model loaded in {self.load_time:.2f}s, memory used: {self.model_mem_mb:.1f} MB")

    def annotate(self, pil_image):
        t0 = time.time()
        inputs = self.processor(images=pil_image, return_tensors="pt").to(self.device)
        generated_ids = self.model.generate(**inputs, max_new_tokens=50)
        caption = self.processor.batch_decode(generated_ids, skip_special_tokens=True)[0].strip()
        t1 = time.time()
        self.last_infer_time = t1 - t0
        return caption

# --- Dummy fallback if no model is specified ---
class DummyBLIP2:
    model_mem_mb = 0
    last_infer_time = 0
    def annotate(self, pil_image):
        return "No model specified. Please provide a valid BLIP-2 model path:\n--model <path_to_model>"

# --- Select model based on argument ---
if args.model:
    import torch
    blip2_model = BLIP2Annotator(args.model)
else:
    blip2_model = DummyBLIP2()

def frame_to_timecode(frame_idx, fps):
    if fps == 0:
        return "00:00:00.000"
    total_seconds = frame_idx / fps
    hours = int(total_seconds // 3600)
    minutes = int((total_seconds % 3600) // 60)
    seconds = int(total_seconds % 60)
    milliseconds = int((total_seconds - int(total_seconds)) * 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

class VideoAnnotator(QWidget):
    def __init__(self):
        super().__init__()
        self.setFocusPolicy(Qt.StrongFocus)
        self.setFocus()
        self.video_path = None
        self.cap = None
        self.frame_count = 0
        self.current_frame_idx = 0
        self.fps = 0
        self.timer = QTimer()
        self.timer.timeout.connect(self.next_frame)
        self.is_playing = False
        self.filename_title = ""
        self.init_ui()

    def init_ui(self):
        layout = QVBoxLayout()

        # 1. Video display (top)
        self.video_label = QLabel("Load a video to begin.")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        layout.addWidget(self.video_label, stretch=10)  # Large stretch for video

        # 2. Timeline slider (beneath video)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderPressed.connect(self.pause)
        self.slider.sliderReleased.connect(self.slider_released)
        self.slider.valueChanged.connect(self.slider_scrub)  # <-- Add this line
        layout.addWidget(self.slider)

        # 3. Three buttons (beneath timeline)
        btn_layout = QHBoxLayout()
        self.load_btn = QPushButton("Load Video")
        self.load_btn.clicked.connect(self.load_video)
        btn_layout.addWidget(self.load_btn)

        self.play_btn = QPushButton("Play")
        self.play_btn.clicked.connect(self.toggle_play)
        self.play_btn.setEnabled(False)
        btn_layout.addWidget(self.play_btn)

        self.annotate_btn = QPushButton("Caption")
        self.annotate_btn.clicked.connect(self.annotate_frame)
        self.annotate_btn.setEnabled(False)
        btn_layout.addWidget(self.annotate_btn)

        layout.addLayout(btn_layout)

        # 4. Text field (bottom, only 2 lines tall)
        self.annotation_field = QTextEdit()
        self.annotation_field.setReadOnly(True)
        self.annotation_field.setMaximumHeight(self.annotation_field.fontMetrics().height() * 2 + 12)
        layout.addWidget(self.annotation_field)

        self.setLayout(layout)
        self.update_titlebar()

    def slider_scrub(self, idx):
        if self.slider.isSliderDown():
            self.show_frame(idx)

    def update_titlebar(self):
        mem_info = ""
        infer_info = ""
        if hasattr(blip2_model, "model_mem_mb") and blip2_model.model_mem_mb:
            mem_info = f" | Model: {blip2_model.model_mem_mb:.1f} MB"
        if hasattr(blip2_model, "last_infer_time") and blip2_model.last_infer_time:
            infer_info = f" | Last infer: {blip2_model.last_infer_time:.2f}s"
        if self.filename_title:
            tc = frame_to_timecode(self.current_frame_idx, self.fps)
            self.setWindowTitle(f"{self.filename_title} - {tc}{mem_info}{infer_info}")
        else:
            self.setWindowTitle(f"Video Annotator{mem_info}{infer_info}")

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
            self.annotate_btn.setEnabled(True)
            self.current_frame_idx = 0
            self.filename_title = os.path.splitext(os.path.basename(self.video_path))[0]
            self.show_frame(self.current_frame_idx)

    def show_frame(self, idx):
        if not self.cap:
            return
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = self.cap.read()
        if ret:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb.shape
            bytes_per_line = ch * w
            qt_image = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
            pixmap = QPixmap.fromImage(qt_image)

            # Scale pixmap to fit label while keeping aspect ratio
            label_w = self.video_label.width()
            label_h = self.video_label.height()
            pixmap = pixmap.scaled(label_w, label_h, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            self.video_label.setPixmap(pixmap)

            self.slider.blockSignals(True)
            self.slider.setValue(idx)
            self.slider.blockSignals(False)
            self.current_frame_idx = idx
            self.update_titlebar()

    def resizeEvent(self, event):
        if self.cap:
            self.show_frame(self.current_frame_idx)
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
        if self.current_frame_idx < self.frame_count - 1:
            self.current_frame_idx += 1
            self.show_frame(self.current_frame_idx)
        else:
            self.pause()

    def slider_released(self):
        idx = self.slider.value()
        self.show_frame(idx)

    def annotate_frame(self):
        if not self.cap:
            return
        self.annotate_btn.setText("Captioning...")
        self.annotate_btn.setEnabled(False)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, self.current_frame_idx)
        ret, frame = self.cap.read()
        if ret:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            pil_img = Image.fromarray(rgb)
            # Start annotation in a separate thread
            self.annotate_thread = AnnotateThread(pil_img, blip2_model)
            self.annotate_thread.finished.connect(self.on_annotation_finished)
            self.annotate_thread.start()
        else:
            self.annotate_btn.setText("Caption")
            self.annotate_btn.setEnabled(True)

    def on_annotation_finished(self, annotation):
        self.annotation_field.setText(annotation)
        self.update_titlebar()
        self.annotate_btn.setText("Caption")
        self.annotate_btn.setEnabled(True)

    def keyPressEvent(self, event):
        key = event.key()
        modifiers = event.modifiers()

        # Spacebar (cross-platform)
        if key in (Qt.Key_Space, 0x20):  # 0x20 is space on some platforms
            self.toggle_play()
            return

        # Annotate (A)
        if key == Qt.Key_A:
            if self.annotate_btn.isEnabled():
                self.annotate_btn.click()
            return

        # Load Video (V)
        if key == Qt.Key_V:
            self.load_btn.click()
            return

        # Arrow keys for timeline navigation
        if key == Qt.Key_Left:
            if self.fps > 0:
                step = int(self.fps * (10 if modifiers & Qt.ShiftModifier else 1))
                new_idx = max(0, self.current_frame_idx - step)
                self.show_frame(new_idx)
            return

        if key == Qt.Key_Right:
            if self.fps > 0:
                step = int(self.fps * (10 if modifiers & Qt.ShiftModifier else 1))
                new_idx = min(self.frame_count - 1, self.current_frame_idx + step)
                self.show_frame(new_idx)
            return

        super().keyPressEvent(event)

def showEvent(self, event):
    self.setFocus()
    super().showEvent(event)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoAnnotator()
    window.resize(800, 600)
    window.show()
    sys.exit(app.exec_())