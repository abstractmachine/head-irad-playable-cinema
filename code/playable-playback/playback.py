# required libraries for the application
import sys, os
import argparse
# OpenCV is used for video processing and frame manipulation
import cv2
# torch is the core library for PyTorch, used for tensor operations and model inference
import torch
# Ultralytics YOLOv8 is a library for object detection and segmentation
from ultralytics import YOLO
# PyQt5 is used for creating the GUI application
from PyQt5.QtWidgets import (
	QApplication, QWidget, QPushButton, QVBoxLayout, QLabel, QSlider
)
from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QImage, QPixmap

# set the normal and fast seek amounts
NORMAL_SEEK_AMOUNT = 1000  # 1 second in milliseconds
FAST_SEEK_AMOUNT = 10000     # 10 seconds in milliseconds

# Parse command line arguments
parser = argparse.ArgumentParser(description='Playable Cinema - Real-Time Video Prediction')
parser.add_argument('--model', '-m', 
                   default='model.pt', 
                   help='Path to the YOLO model file (default: model.pt)')
args = parser.parse_args()

# Load YOLO model for real-time prediction
model = YOLO(args.model)
device = "mps" if torch.backends.mps.is_available() else "cpu"
model.to(device)

# Initialize Qt app
app = QApplication(sys.argv)
# Create a resizable window for the application
class ResizableWindow(QWidget):
	def resizeEvent(self, event):
		w, h = self.width(), self.height()
		print(f"Window resized to: {w} x {h}")
		super().resizeEvent(event)

	def keyPressEvent(self, event):
		if event.key() == Qt.Key_Space:
			toggle_play_pause()
		elif event.key() in (Qt.Key_Left, Qt.Key_J):  # Left arrow or A key
			# Check if shift key is held - if so, seek 3 seconds back, otherwise 1 second
			seek_amount = FAST_SEEK_AMOUNT if event.modifiers() & Qt.ShiftModifier else NORMAL_SEEK_AMOUNT
			new_time = max(0, current_time_ms - seek_amount)
			seek(new_time)
			slider.setValue(new_time)
		elif event.key() in (Qt.Key_Right, Qt.Key_L):  # Right arrow or D key
			# Check if shift key is held - if so, seek 3 seconds forward, otherwise 1 second
			seek_amount = FAST_SEEK_AMOUNT if event.modifiers() & Qt.ShiftModifier else NORMAL_SEEK_AMOUNT
			new_time = min(duration_ms, current_time_ms + seek_amount)
			seek(new_time)
			slider.setValue(new_time)
		else:
			super().keyPressEvent(event)

window = ResizableWindow()
# Set the title of the window
window.setWindowTitle("Playable Cinema – Real-Time Prediction")

# Set up video capture from file
video_path = os.path.abspath("video.mp4")
cap = cv2.VideoCapture(video_path)
fps = cap.get(cv2.CAP_PROP_FPS)
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
duration_ms = int((total_frames / fps) * 1000)

# UI elements for displaying video and controls
video_label = QLabel()
video_label.setAlignment(Qt.AlignCenter)
# Replace both play_button and pause_button with a single toggle button
play_pause_button = QPushButton("Play")
# Slider for seeking through the video
slider = QSlider(Qt.Horizontal)
slider.setRange(0, duration_ms)

# the video timer for frame updates
play_timer = QTimer()

# Track current playback time and buffer for current frame
current_time_ms = 0
frame_buffer = None
is_playing = False  # Track playback state
is_scrubbing = False  # Track if user is dragging the slider

# ---- FRAME PROCESSING ----

# Function to display a frame in the video label
def show_frame(frame):
	rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
	h, w, ch = rgb.shape
	image = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
	# scale the image to fit the label while maintaining aspect ratio
	video_label.setPixmap(QPixmap.fromImage(image).scaled(
		video_label.width(), video_label.height(), Qt.KeepAspectRatio, Qt.SmoothTransformation
	))

# Function to show frame without YOLO inference (for scrubbing)
def show_frame_without_inference(frame):
	show_frame(frame)

# Function to display the next frame in the video
def display_frame():
	global current_time_ms, frame_buffer
	ret, frame = cap.read()
	if not ret:
		# stop playback if no frame is returned
		play_timer.stop()
		return

	frame_buffer = frame.copy()

	# Run YOLO prediction on the current frame
	result = model.predict(frame_buffer, verbose=False)[0]
	annotated = result.plot()
	show_frame(annotated)

	# Update the slider position based on current playback time
	current_time_ms += int(1000 / fps)
	slider.setValue(current_time_ms)
	update_timecode()  # Update titlebar

def ms_to_timecode(ms):
	"""Convert milliseconds to HH:MM:SS format."""
	seconds = ms // 1000
	h = seconds // 3600
	m = (seconds % 3600) // 60
	s = seconds % 60
	return f"{h:02}:{m:02}:{s:02}"

def update_timecode():
	current = ms_to_timecode(current_time_ms)
	total = ms_to_timecode(duration_ms)
	window.setWindowTitle(f"Playable Cinema – {current} / {total}")

# ---- CONTROLS ----

def toggle_play_pause():
	global is_playing
	if is_playing:
		play_timer.stop()
		play_pause_button.setText("Play")
		is_playing = False
	else:
		play_timer.start(int(1000 / fps))
		play_pause_button.setText("Pause")
		is_playing = True

# Function to seek to a specific position in the video
def seek(position_ms, run_inference=True):
	# variables to track current playback time and frame buffer
	global current_time_ms, frame_buffer
	current_time_ms = position_ms
	frame_index = int((position_ms / 1000) * fps)
	# set the video capture to the specific frame index
	cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
	ret, frame = cap.read()
	# if frame is successfully read, process it
	if ret:
		# copy the frame to the buffer for processing
		frame_buffer = frame.copy()
		
		if run_inference:
			# Run YOLO prediction on the current frame
			result = model(frame_buffer)[0]
			# Annotate the frame with predictions
			annotated = result.plot()
			# Display the annotated frame in the video label
			show_frame(annotated)
		else:
			# Show frame without inference during scrubbing
			show_frame_without_inference(frame_buffer)
	update_timecode()  # Update titlebar

# ---- SIGNALS ----

# Function to handle slider press (start scrubbing)
def slider_pressed():
	global is_scrubbing
	is_scrubbing = True
	# Pause playback during scrubbing
	if is_playing:
		play_timer.stop()

# Function to handle slider release (end scrubbing)
def slider_released():
	global is_scrubbing
	is_scrubbing = False
	# Run inference on the final position
	seek(slider.value(), run_inference=True)
	# Resume playback if it was playing before scrubbing
	if is_playing:
		play_timer.start(int(1000 / fps))

# Function to handle slider movement during scrubbing
def slider_moved(value):
	if is_scrubbing:
		# Show frame without inference during scrubbing
		seek(value, run_inference=False)
	else:
		# Normal seek with inference
		seek(value, run_inference=True)

# Connect the button to the toggle function
play_pause_button.clicked.connect(toggle_play_pause)
slider.sliderPressed.connect(slider_pressed)
slider.sliderReleased.connect(slider_released)
slider.sliderMoved.connect(slider_moved)
play_timer.timeout.connect(display_frame)

# ---- LAYOUT ----

# Create the layout for the main window
layout = QVBoxLayout()
layout.addWidget(video_label)
layout.addWidget(slider)
layout.addWidget(play_pause_button)

# Set the layout for the window
window.setLayout(layout)
# default size for the window
window.resize(921, 640)
window.show()

# system exit handler to clean up resources
sys.exit(app.exec_())
