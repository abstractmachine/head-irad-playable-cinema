import os
import csv
import re

from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QTableWidget, QTableWidgetItem, QFileDialog, QApplication, QTextEdit
)
from PyQt5.QtCore import Qt, pyqtSignal, QThread, QObject, QTimer
from PyQt5.QtWidgets import QComboBox, QLineEdit

from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

JUMP_FRAME_PADDING_PLAYBACK = 0  # Number of frames to pad when jumping in playback mode
JUMP_FRAME_PADDING_DETECTION = 5  # Number of frames to pad when jumping in detection mode

# Function to parse command line arguments for detector options
# Returns a dictionary of detector arguments based on the provided string
def parse_detector_args(arg_string):
    # Supported options mapping: CLI arg -> detector kwarg
    option_map = {
        '-t': 'threshold',
        '--threshold': 'threshold',
        '-c': 'min_content_val',
        '--min-content-val': 'min_content_val',
        '-w': 'weights',
        '--weights': 'weights',
        '-l': 'luma_only',
        '--luma-only': 'luma_only',
        '-k': 'kernel_size',
        '--kernel-size': 'kernel_size',
        '-m': 'min_scene_len',
        '--min-scene-len': 'min_scene_len',
        '-f': 'frame_window',
        '--frame-window': 'frame_window',
        '-s': 'size',
        '--size': 'size',
        '--fade-bias': 'fade_bias',
        '--add-last-scene': 'add_last_scene',
        '--bins': 'bins',
        '-b': 'bins',
        '--lowpass': 'lowpass',
    }
    # Regex for options
    pattern = r'(-{1,2}\w+)(?:\s+([^\s-][^-\s]*))?'
    matches = re.findall(pattern, arg_string)
    kwargs = {}
    for opt, val in matches:
        key = option_map.get(opt)
        if key:
            # Special handling for weights (4 floats)
            if key == 'weights':
                weights_match = re.findall(r'-w ([\d\.]+) ([\d\.]+) ([\d\.]+) ([\d\.]+)', arg_string)
                if weights_match:
                    kwargs['weights'] = tuple(map(float, weights_match[0]))
            # Special handling for luma_only (flag)
            elif key == 'luma_only':
                kwargs['luma_only'] = True
            # Special handling for add_last_scene (flag)
            elif key == 'add_last_scene':
                kwargs['add_last_scene'] = True
            elif val is not None and val != '':
                # Try to convert to float or int if possible
                try:
                    if '.' in val:
                        kwargs[key] = float(val)
                    else:
                        kwargs[key] = int(val)
                except ValueError:
                    kwargs[key] = val
    return kwargs

# Helper function to create a detector based on the method and args
def create_detector(method, detector_args):
    unused_args = dict(detector_args)  # Track which args are not used

    if method == "detect-adaptive":
        from scenedetect.detectors import AdaptiveDetector
        detector = AdaptiveDetector()
        # Map CLI threshold to adaptive_threshold
        if "threshold" in detector_args:
            detector.adaptive_threshold = detector_args["threshold"]
            unused_args.pop("threshold", None)
        # Set other attributes if present
        for key in ["frame_window", "min_content_val", "weights", "luma_only", "kernel_size", "min_scene_len"]:
            if key in detector_args:
                setattr(detector, key, detector_args[key])
                unused_args.pop(key, None)
    elif method == "detect-content":
        print("detect-content not yet implemented")
        detector = ContentDetector()
    elif method == "detect-hist":
        print("detect-hist not yet implemented")
        detector = ContentDetector()
    elif method == "detect-threshold":
        print("detect-threshold not yet implemented")
        detector = ContentDetector()
    elif method == "detect-hash":
        print("detect-hash not yet implemented")
        detector = ContentDetector()
    else:
        from scenedetect.detectors import ContentDetector
        detector = ContentDetector()

    # Warn about unused args
    if unused_args:
        print(f"Warning: The following detector options were not used: {', '.join(unused_args.keys())}")

    print(f"Detector type: {type(detector).__name__}, threshold: {getattr(detector, 'threshold', None)}")
    print("vars(detector):", vars(detector))

    return detector

# SceneDetectWorker class to run scene detection in a separate thread
class SceneDetectWorker(QObject):
    finished = pyqtSignal(list)

    def __init__(self, video_path, method, weights):
        super().__init__()
        self.video_path = video_path
        self.method = method
        self.weights = weights

    def run(self):
        print("Scene detection started for:", self.video_path)
        scene_list = []
        try:
            video = open_video(self.video_path)
            scene_manager = SceneManager()

            # Parse all options from weights/threshold field
            detector_args = parse_detector_args(self.weights)
            print(f"Parsed detector args: {detector_args}")

            # Create and add the detector using the helper function
            detector = create_detector(self.method, detector_args)
            scene_manager.add_detector(detector)

            scene_manager.detect_scenes(video)
            scene_list = scene_manager.get_scene_list()
        except Exception as e:
            scene_list = [f"Error: {e}"]
        print("Scene detection finished.")
        self.finished.emit(scene_list)

class DetectorWindow(QMainWindow):
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    jump_to_timecode_signal = pyqtSignal(str, bool)  # timecode, is_last_frame

    def __init__(self):
        super().__init__()
        self._pending_save_data = {}
        self.setWindowTitle("Shotlist")
        self.setGeometry(200, 200, 600, 400)

        # Default detections folder: same as app.py
        self.detections_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes")
        os.makedirs(self.detections_folder, exist_ok=True)

        # Animation for "Detecting..."
        self.detecting_timer = QTimer()
        self.detecting_timer.timeout.connect(self.animate_detecting)
        self.detecting_dots = 0

        # Central widget and layout
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Scene table
        self.scene_table = QTableWidget()
        self.scene_table.setColumnCount(3)
        self.scene_table.setHorizontalHeaderLabels(["Start", "End", "Caption"])
        self.scene_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.scene_table.cellClicked.connect(self.on_table_cell_clicked)
        layout.addWidget(self.scene_table)
        self.scene_table.setColumnWidth(0, 110)
        self.scene_table.setColumnWidth(1, 110)
        self.scene_table.setColumnWidth(2, 500)

        # Detect Scenes button
        self.detect_button = QPushButton("Detect")
        self.detect_button.setFixedWidth(120)
        self.detect_button.setMinimumHeight(32)
        self.detect_button.setStyleSheet(
            "text-align: center; padding-left: 0px; padding-top: 3px; padding-bottom: 6px;"
        )
        self.detect_button.setEnabled(False)

        # Folder button
        self.folder_button = QPushButton("Folder")
        self.folder_button.setFixedWidth(100)
        self.folder_button.setMinimumHeight(32)
        self.folder_button.clicked.connect(self.select_detections_folder)

        # Detection method dropdown
        self.method_dropdown = QComboBox()
        self.method_dropdown.addItems([
            "detect-adaptive",
            "detect-content",
            "detect-hash",
            "detect-hist",
            "detect-threshold"
        ])
        self.method_dropdown.setFixedWidth(150)

        # Weights/threshold field
        self.weights_field = QLineEdit("-w 1.0 1.0 1.0 0.0 -t 32")
        self.weights_field.setFixedWidth(180)
        # center the text
        self.weights_field.setAlignment(Qt.AlignCenter)
        self.weights_field.setToolTip("Set PySceneDetect parameters.\nSee documentation for details.\nExamples:\nweights: -w 1.0 1.0 1.0 0.0\nthreshold: -t 3.2")

        # Delete button
        self.delete_button = QPushButton("Delete")
        self.delete_button.setFixedWidth(100)
        self.delete_button.setMinimumHeight(32)
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_scene_csv)

        button_layout = QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(self.folder_button)
        button_layout.addWidget(self.method_dropdown)
        button_layout.addWidget(self.weights_field)
        button_layout.addWidget(self.detect_button)
        button_layout.addWidget(self.delete_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)
        
        self.detect_button.clicked.connect(self.on_detect_scenes)

        self.setCentralWidget(central_widget)

        # We start with no video path
        self.video_path = None
        self.current_csv_path = None  # Track loaded CSV

        # Connect preference signals
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)

        # Thread reference
        self.thread = None

    def select_detections_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Scene Detections Folder", self.detections_folder)
        if folder:
            self.detections_folder = folder

    def load_scene_detections(self, csv_path):
        with open(csv_path, "r", newline="") as f:
            reader = csv.reader(f)
            headers = next(reader, None)
            rows = list(reader)
        self.scene_table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j in range(3):
                self.scene_table.setItem(i, j, QTableWidgetItem(row[j] if j < len(row) else ""))
        self.current_csv_path = csv_path
        self.delete_button.setEnabled(True)

    def on_detect_scenes(self):
        self.detect_button.setEnabled(False)
        self.detecting_dots = 0
        self.detect_button.setText("Detecting")
        self.detect_button.setStyleSheet(
            "text-align: left; color: grey; padding-left: 30px; padding-top: 3px; padding-bottom: 6px;"
        )
        self.detecting_timer.start(500) # update every 0.5s

        self.scene_table.setRowCount(0)
        if not self.video_path or not os.path.exists(self.video_path):
            self.scene_table.setRowCount(1)
            print("No video loaded or file not found.")
            self.detect_button.setEnabled(True)
            self.detect_button.setText("Detect Scenes")
            self.detect_button.setStyleSheet(
                "text-align: center; color: black; padding-left: 0px; padding-top: 3px; padding-bottom: 6px;"
            )
            self.detecting_timer.stop()
            return

        # Get method and weights/threshold
        method = self.method_dropdown.currentText()
        weights_text = self.weights_field.text().strip()

        print(f"Starting scene detection thread with method: {method}, params: {weights_text}")
        self.worker = SceneDetectWorker(self.video_path, method, weights_text)
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_scene_detected)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.on_detection_finished)
        self.thread.start()

    def animate_detecting(self):
        self.detecting_dots = (self.detecting_dots + 1) % 4
        self.detect_button.setText("Detecting" + "." * self.detecting_dots)
        self.detect_button.setStyleSheet(
            "text-align: left; color: grey; padding-left: 30px; padding-top: 3px; padding-bottom: 6px;"
        )

    def on_detection_finished(self):
        self.detect_button.setEnabled(True)
        self.detect_button.setText("Detect Scenes")
        self.detect_button.setStyleSheet(
            "text-align: center; color: black; padding-left: 0px; padding-top: 3px; padding-bottom: 6px;"
        )
        self.detecting_timer.stop()

    def on_scene_detected(self, scene_list):
        self.on_detection_finished()
        if not scene_list or (isinstance(scene_list[0], str) and scene_list[0].startswith("Error:")):
            self.scene_table.setRowCount(1)
            print("No scenes detected or an error occurred:", scene_list[0] if isinstance(scene_list, list) else scene_list)
            self.scene_table.setItem(0, 0, QTableWidgetItem(""))
            self.scene_table.setItem(0, 1, QTableWidgetItem(""))
            self.scene_table.setItem(0, 2, QTableWidgetItem(""))
            self.delete_button.setEnabled(False)  # <-- Disable on error
            return

        # --- Padding system ---
        # Estimate FPS for padding
        fps = 25
        if self.video_path:
            try:
                video = open_video(self.video_path)
                if hasattr(video, "frame_rate"):
                    fps = video.frame_rate
            except Exception:
                pass
        frame_duration = int(1000 / fps)
        start_padding = JUMP_FRAME_PADDING_DETECTION * frame_duration
        end_padding = JUMP_FRAME_PADDING_DETECTION * frame_duration

        self.scene_table.setRowCount(len(scene_list))
        csv_rows = []
        for i, scene in enumerate(scene_list):
            # Get original timecodes in ms
            start_tc = scene[0].get_timecode()
            end_tc = scene[1].get_timecode()

            # Convert timecodes to ms
            def tc_to_ms(tc):
                parts = tc.split(":")
                if len(parts) == 3:
                    h = int(parts[0])
                    m = int(parts[1])
                    s = float(parts[2])
                    return int((h * 3600 + m * 60 + s) * 1000)
                return 0

            start_ms = tc_to_ms(start_tc)
            end_ms = tc_to_ms(end_tc)

            # Apply padding: start later, end earlier
            padded_start_ms = start_ms + start_padding
            padded_end_ms = max(end_ms - end_padding, 0)

            # Convert back to timecode string
            def ms_to_tc(ms):
                total_seconds = ms / 1000.0
                h = int(total_seconds // 3600)
                m = int((total_seconds % 3600) // 60)
                s = total_seconds % 60
                return f"{h:02}:{m:02}:{s:06.3f}"

            padded_start_tc = ms_to_tc(padded_start_ms)
            padded_end_tc = ms_to_tc(padded_end_ms)

            self.scene_table.setItem(i, 0, QTableWidgetItem(padded_start_tc))
            self.scene_table.setItem(i, 1, QTableWidgetItem(padded_end_tc))
            self.scene_table.setItem(i, 2, QTableWidgetItem(""))  # Empty caption for now
            csv_rows.append([padded_start_tc, padded_end_tc, ""])  # Save caption as empty for now

        # Save to CSV
        if self.video_path:
            base = os.path.basename(self.video_path)
            name, _ = os.path.splitext(base)
            out_path = os.path.join(self.detections_folder, f"{name}.csv")
            with open(out_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Start", "End", "Caption"])
                writer.writerows(csv_rows)
            self.current_csv_path = out_path
            self.delete_button.setEnabled(True)

    def on_table_cell_clicked(self, row, col):
        start_tc = self.scene_table.item(row, 0).text()
        end_tc = self.scene_table.item(row, 1).text()
        # Signal to player window (assumes you have a reference to it)
        if col == 0:
            self.jump_to_timecode(start_tc)
        elif col == 1:
            self.jump_to_timecode(end_tc, is_last_frame=True)
        # Caption column (col == 2) and others can be handled later

    def jump_to_timecode(self, timecode, is_last_frame=False):
        parts = timecode.split(":")
        if len(parts) == 3:
            h = int(parts[0])
            m = int(parts[1])
            s = float(parts[2])
            ms = int((h * 3600 + m * 60 + s) * 1000)

            fps = 25
            if self.video_path:
                try:
                    from scenedetect import open_video
                    video = open_video(self.video_path)
                    if hasattr(video, "frame_rate"):
                        fps = video.frame_rate
                except Exception:
                    pass

            frame_duration = int(1000 / fps)
            jump_frame_padding = JUMP_FRAME_PADDING_PLAYBACK * frame_duration

            if is_last_frame:
                ms = max(ms - jump_frame_padding, 0)
            else:
                ms = ms + jump_frame_padding

            # Convert ms back to timecode string
            total_seconds = ms / 1000.0
            h_new = int(total_seconds // 3600)
            m_new = int((total_seconds % 3600) // 60)
            s_new = total_seconds % 60
            padded_timecode = f"{h_new:02}:{m_new:02}:{s_new:06.3f}"

            self.jump_to_timecode_signal.emit(padded_timecode, is_last_frame)
        else:
            self.jump_to_timecode_signal.emit(timecode, is_last_frame)

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "x": pos.x(),
            "y": pos.y(),
            "width": size.width(),
            "height": size.height(),
            "col0_width": self.scene_table.columnWidth(0),
            "col1_width": self.scene_table.columnWidth(1),
            "col2_width": self.scene_table.columnWidth(2),
            "detections_folder": self.detections_folder,
            "weights_field": self.weights_field.text(),
            "method_selected": self.method_dropdown.currentText()  # <-- Add this line
        }

    def on_request_load(self, data):
        if "x" in data and "y" in data:
            self.move(data["x"], data["y"])
        if "width" in data and "height" in data:
            self.resize(data["width"], data["height"])
        if "col0_width" in data:
            self.scene_table.setColumnWidth(0, data["col0_width"])
        if "col1_width" in data:
            self.scene_table.setColumnWidth(1, data["col1_width"])
        if "col2_width" in data:
            self.scene_table.setColumnWidth(2, data["col2_width"])
        if "detections_folder" in data:
            self.detections_folder = data["detections_folder"]
        if "weights_field" in data:
            self.weights_field.setText(data["weights_field"])
        if "method_selected" in data:
            idx = self.method_dropdown.findText(data["method_selected"])
            if idx != -1:
                self.method_dropdown.setCurrentIndex(idx)  # <-- Add this block

    def process_video(self, video_path):
        if video_path:
            self.video_path = video_path
            base = os.path.basename(video_path)
            name, _ = os.path.splitext(base)
            csv_path = os.path.join(self.detections_folder, f"{name}.csv")
            if os.path.exists(csv_path):
                self.load_scene_detections(csv_path)
            else:
                self.scene_table.setRowCount(0)
                self.current_csv_path = None
                self.delete_button.setEnabled(False)
            self.detect_button.setEnabled(True)
        else:
            self.video_path = None
            self.scene_table.setRowCount(0)
            self.current_csv_path = None
            self.delete_button.setEnabled(False)
            self.detect_button.setEnabled(False)

    def delete_scene_csv(self):
        if self.current_csv_path and os.path.exists(self.current_csv_path):
            os.remove(self.current_csv_path)
        self.scene_table.setRowCount(0)
        self.current_csv_path = None
        self.delete_button.setEnabled(False)

    def clear_table_selection(self):
        self.scene_table.clearSelection()

    def handle_global_key(self, event):
        focus_widget = QApplication.focusWidget()
        if not isinstance(focus_widget, QTextEdit):
            self.keyPressEvent(event)