import sys, os
import cv2
from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QSlider,
    QFileDialog, QHBoxLayout, QTextEdit
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap

NORMAL_SEEK_AMOUNT = 1000
FAST_SEEK_AMOUNT = 10000

app = QApplication(sys.argv)

# Crée le dossier Dataset pour stocker les images annotées
DATASET_FOLDER = os.path.join(os.path.dirname(__file__), "Dataset")
os.makedirs(DATASET_FOLDER, exist_ok=True)

annotation_counter = 1

class ResizableWindow(QWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Space:
            toggle_play_pause()
        elif event.key() == Qt.Key_Left:
            seek_amount = FAST_SEEK_AMOUNT if event.modifiers() & Qt.ShiftModifier else NORMAL_SEEK_AMOUNT
            new_time = max(0, current_time_ms - seek_amount)
            seek(new_time)
            slider.setValue(new_time)
        elif event.key() == Qt.Key_Right:
            seek_amount = FAST_SEEK_AMOUNT if event.modifiers() & Qt.ShiftModifier else NORMAL_SEEK_AMOUNT
            new_time = min(duration_ms, current_time_ms + seek_amount)
            seek(new_time)
            slider.setValue(new_time)
        else:
            super().keyPressEvent(event)

window = ResizableWindow()
window.setWindowTitle("Annotation vidéo – Dataset Builder")

cap = None
fps = 30
duration_ms = 0
current_time_ms = 0
frame_buffer = None
is_playing = False

video_label = QLabel(alignment=Qt.AlignCenter)
play_pause_button = QPushButton("Play")
slider = QSlider(Qt.Horizontal)
play_timer = QTimer()

# Zone de texte pour les annotations
text_input = QTextEdit()
text_input.setPlaceholderText("Écrivez ici pour annoter l'image affichée...")
text_input.setFixedHeight(60)

annotate_button = QPushButton("Annoter")

def ms_to_timecode(ms):
    seconds = ms // 1000
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02}:{m:02}:{s:02}"

def update_timecode():
    current = ms_to_timecode(current_time_ms)
    total = ms_to_timecode(duration_ms)
    window.setWindowTitle(f"Annotation vidéo – {current} / {total}")

def show_frame(frame):
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    h, w, ch = rgb.shape
    image = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
    video_label.setPixmap(QPixmap.fromImage(image).scaled(
        video_label.width(), video_label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation
    ))

def display_frame():
    global current_time_ms, frame_buffer
    if not cap:
        return
    ret, frame = cap.read()
    if not ret:
        play_timer.stop()
        return
    frame_buffer = frame.copy()
    show_frame(frame_buffer)
    current_time_ms += int(1000 / fps)
    slider.setValue(current_time_ms)
    update_timecode()

def toggle_play_pause():
    global is_playing
    if not cap:
        return
    if is_playing:
        play_timer.stop()
        play_pause_button.setText("Play")
        is_playing = False
    else:
        play_timer.start(int(1000 / fps))
        play_pause_button.setText("Pause")
        is_playing = True

def seek(position_ms):
    global current_time_ms, frame_buffer
    if not cap:
        return
    current_time_ms = position_ms
    frame_index = int((position_ms / 1000) * fps)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
    ret, frame = cap.read()
    if ret:
        frame_buffer = frame.copy()
        show_frame(frame_buffer)
    update_timecode()

def load_video():
    global cap, fps, duration_ms, current_time_ms
    play_timer.stop()
    play_pause_button.setText("Play")
    current_time_ms = 0
    filename, _ = QFileDialog.getOpenFileName(window, "Sélectionner une vidéo", "", "Fichiers vidéo (*.mp4 *.avi *.mov)")
    if filename:
        if cap:
            cap.release()
        cap = cv2.VideoCapture(filename)
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_ms = int((total_frames / fps) * 1000)
        slider.setRange(0, duration_ms)
        seek(0)


def annotate_current_frame():
    # Verify that there is a frame to annotate
    global annotation_counter
    if frame_buffer is None:
        return
    # Verify that there is an annotation text
    text = text_input.toPlainText().strip()
    if not text:
        return

    # Save the image in jpg format and the text in txt format in the Dataset folder
    image_name = f"image_{annotation_counter:04}.jpg"
    text_name = f"image_{annotation_counter:04}.txt"
    image_path = os.path.join(DATASET_FOLDER, image_name)
    text_path = os.path.join(DATASET_FOLDER, text_name)
    cv2.imwrite(image_path, frame_buffer)
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text)
    # Increment the annotation counter and clear the text input
    annotation_counter += 1
    text_input.clear()
    print(f"Annotation enregistrée : {image_name} / {text}")

# Connexions
play_pause_button.clicked.connect(toggle_play_pause)
slider.sliderMoved.connect(seek)
play_timer.timeout.connect(display_frame)
load_button = QPushButton("Charger une vidéo")
load_button.clicked.connect(load_video)
annotate_button.clicked.connect(annotate_current_frame)

# Mise en page
controls_layout = QHBoxLayout()
controls_layout.addWidget(load_button)
controls_layout.addWidget(play_pause_button)

layout = QVBoxLayout()
layout.addWidget(video_label)
layout.addWidget(slider)
layout.addLayout(controls_layout)
layout.addWidget(text_input)
layout.addWidget(annotate_button)

window.setLayout(layout)
window.resize(960, 800)
window.show()

sys.exit(app.exec_())
