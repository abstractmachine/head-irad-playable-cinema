import sys, os
import cv2
from PyQt5.QtWidgets import (
    QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QSlider,
    QFileDialog, QHBoxLayout, QTextEdit
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap

# Custom QTextEdit that exits writing mode on Enter
class CustomTextEdit(QTextEdit):
    def keyPressEvent(self, event):
        # If Enter or Return is pressed, exit writing mode immediately
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            exit_writing_mode()
            return  # Don't process the Enter key further
        else:
            # For all other keys, handle normally
            super().keyPressEvent(event)

NORMAL_SEEK_AMOUNT = 100
FAST_SEEK_AMOUNT = 5000

# Mode tracking
NAVIGATION_MODE = "navigation"
WRITING_MODE = "writing"
current_mode = NAVIGATION_MODE

app = QApplication(sys.argv)

# Crée le dossier Dataset pour stocker les images annotées
DATASET_FOLDER = os.path.join(os.path.dirname(__file__), "Dataset")
os.makedirs(DATASET_FOLDER, exist_ok=True)

class ResizableWindow(QWidget):
    def resizeEvent(self, event):
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        global current_mode
        
        # Handle mode switching with ENTER key (only for entering writing mode)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if current_mode == NAVIGATION_MODE:
                # Enter writing mode
                enter_writing_mode()
            # Note: Exit from writing mode is now handled by CustomTextEdit
            return
        
        # Navigation mode controls
        if current_mode == NAVIGATION_MODE:
            if event.key() == Qt.Key_Space:
                toggle_play_pause()
            elif event.key() == Qt.Key_Left:
                # Move one frame back or 1 second with shift
                if event.modifiers() & Qt.ShiftModifier:
                    seek_amount = FAST_SEEK_AMOUNT
                else:
                    seek_amount = NORMAL_SEEK_AMOUNT
                new_time = max(0, current_time_ms - seek_amount)
                seek(new_time)
                slider.setValue(new_time)
            elif event.key() == Qt.Key_Right:
                # Move one frame forward or 1 second with shift
                if event.modifiers() & Qt.ShiftModifier:
                    seek_amount = FAST_SEEK_AMOUNT
                else:
                    seek_amount = NORMAL_SEEK_AMOUNT
                new_time = min(duration_ms, current_time_ms + seek_amount)
                seek(new_time)
                slider.setValue(new_time)
            elif event.key() == Qt.Key_A:
                # Press annotate button
                annotate_current_frame()
            elif event.key() == Qt.Key_B or event.key() == Qt.Key_V:
                # Open browser to select video
                load_video()
        
        # In writing mode, other keys are handled by the CustomTextEdit
        else:
            super().keyPressEvent(event)

window = ResizableWindow()
window.setWindowTitle("Annotation vidéo – Dataset Builder")
window.setFocusPolicy(Qt.StrongFocus)

cap = None
fps = 30
duration_ms = 0
current_time_ms = 0
frame_buffer = None
is_playing = False
current_video_path = None  # Store video path separately

video_label = QLabel(alignment=Qt.AlignCenter)
video_label.setFocusPolicy(Qt.NoFocus)  # Prevent taking focus

play_pause_button = QPushButton("Play")
play_pause_button.setFocusPolicy(Qt.NoFocus)  # Prevent taking focus

slider = QSlider(Qt.Horizontal)
slider.setFocusPolicy(Qt.NoFocus)  # Prevent taking focus

play_timer = QTimer()

# Zone de texte pour les annotations
text_input = CustomTextEdit()
text_input.setPlaceholderText("Appuyez sur ENTRÉE pour commencer à écrire...")
text_input.setFixedHeight(60)
text_input.setFocusPolicy(Qt.ClickFocus)  # Only get focus when clicked or programmatically

annotate_button = QPushButton("Annoter")
annotate_button.setFocusPolicy(Qt.NoFocus)  # Prevent taking focus

def ms_to_timecode(ms):
    seconds = ms // 1000
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02}:{m:02}:{s:02}"

def update_timecode():
    current = ms_to_timecode(current_time_ms)
    total = ms_to_timecode(duration_ms)
    mode_text = "✏️ WRITING" if current_mode == WRITING_MODE else "🎮 NAVIGATION"
    window.setWindowTitle(f"Annotation vidéo – {current} / {total} – {mode_text}")

def enter_writing_mode():
    global current_mode
    current_mode = WRITING_MODE
    text_input.setFocus()
    text_input.setStyleSheet("QTextEdit { border: 2px solid #007ACC; background-color: #f0f8ff; }")
    update_timecode()
    print("Entered WRITING mode - type your annotation")

def exit_writing_mode():
    global current_mode
    current_mode = NAVIGATION_MODE
    window.setFocus()  # Return focus to main window
    text_input.setStyleSheet("QTextEdit { border: 1px solid #ccc; }")
    update_timecode()
    print("Entered NAVIGATION mode - use arrow keys, space, A, B")

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
    global cap, fps, duration_ms, current_time_ms, current_video_path
    play_timer.stop()
    play_pause_button.setText("Play")
    current_time_ms = 0
    filename, _ = QFileDialog.getOpenFileName(window, "Sélectionner une vidéo", "", "Fichiers vidéo (*.mp4 *.avi *.mov)")
    if filename:
        if cap:
            cap.release()
        cap = cv2.VideoCapture(filename)
        # Store the video path in global variable
        current_video_path = filename
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        duration_ms = int((total_frames / fps) * 1000)
        slider.setRange(0, duration_ms)
        seek(0)


def annotate_current_frame():
    # Verify that there is a frame to annotate
    if frame_buffer is None:
        return
    # Verify that there is an annotation text
    text = text_input.toPlainText().strip()
    if not text:
        return

    # Get the current video filename (without extension) and timecode
    video_filename = "unknown"
    if current_video_path:
        video_filename = os.path.splitext(os.path.basename(current_video_path))[0]
    
    # Convert current time to hours, minutes, seconds
    seconds_total = current_time_ms // 1000
    hours = seconds_total // 3600
    minutes = (seconds_total % 3600) // 60
    seconds = seconds_total % 60
    
    # Create filenames with timecode format
    base_name = f"{video_filename}-{hours:02d}-{minutes:02d}-{seconds:02d}"
    image_name = f"{base_name}.jpg"
    text_name = f"{base_name}.txt"
    
    image_path = os.path.join(DATASET_FOLDER, image_name)
    text_path = os.path.join(DATASET_FOLDER, text_name)
    
    cv2.imwrite(image_path, frame_buffer)
    with open(text_path, "w", encoding="utf-8") as f:
        f.write(text)
    
    # DON'T! Clear the text input
    #text_input.clear()
    print(f"Annotation enregistrée : {image_name} / {text}")

# Connexions
play_pause_button.clicked.connect(toggle_play_pause)
slider.sliderMoved.connect(seek)
play_timer.timeout.connect(display_frame)
load_button = QPushButton("Charger une vidéo")
load_button.setFocusPolicy(Qt.NoFocus)  # Prevent taking focus
load_button.clicked.connect(load_video)
annotate_button.clicked.connect(annotate_current_frame)

# Function to ensure window maintains focus
def ensure_window_focus():
    if current_mode == NAVIGATION_MODE:
        window.setFocus()
        window.activateWindow()
        window.raise_()

# Override widget interactions to maintain focus
def focus_safe_toggle():
    toggle_play_pause()
    ensure_window_focus()

def focus_safe_load():
    load_video()
    ensure_window_focus()

def focus_safe_annotate():
    annotate_current_frame()
    ensure_window_focus()

# Reconnect with focus-safe functions
play_pause_button.clicked.disconnect()
load_button.clicked.disconnect()
annotate_button.clicked.disconnect()

play_pause_button.clicked.connect(focus_safe_toggle)
load_button.clicked.connect(focus_safe_load)
annotate_button.clicked.connect(focus_safe_annotate)

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

# Start in navigation mode with proper focus
ensure_window_focus()
update_timecode()  # Show initial mode in title
print("Started in NAVIGATION mode - use arrow keys, space, A, B, ENTER")

sys.exit(app.exec_())
