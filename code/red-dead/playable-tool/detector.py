import csv
import os
import re
import time

from PyQt5.QtCore import Qt, pyqtSignal, QThread, QObject, QTimer
from PyQt5.QtGui import QFont
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QMainWindow,
    QPushButton, QFileDialog, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget
)

from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector

JUMP_FRAME_PADDING_PLAYBACK = 0  # Number of frames to pad when jumping in playback mode
JUMP_FRAME_PADDING_DETECTION = 5  # Number of frames to pad when jumping in detection mode

# Function to parse command line arguments for detector options
def parse_detector_args(arg_string):
    option_map = {
        '-t': 'threshold',
        '--adaptive-threshold': 'adaptive_threshold',
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
    pattern = r'(-{1,2}\w+)(?:\s+([^\s-][^-\s]*))?'
    matches = re.findall(pattern, arg_string)
    kwargs = {}
    for opt, val in matches:
        key = option_map.get(opt)
        if key:
            if key == 'weights':
                weights_match = re.findall(r'-w ([\d\.]+) ([\d\.]+) ([\d\.]+) ([\d\.]+)', arg_string)
                if weights_match:
                    kwargs['weights'] = tuple(map(float, weights_match[0]))
            elif key == 'luma_only':
                kwargs['luma_only'] = True
            elif key == 'add_last_scene':
                kwargs['add_last_scene'] = True
            elif val is not None and val != '':
                try:
                    if '.' in val:
                        kwargs[key] = float(val)
                    else:
                        kwargs[key] = int(val)
                except ValueError:
                    kwargs[key] = val
    return kwargs

def create_detector(method, detector_args):
    unused_args = dict(detector_args)
    if method == "detect-adaptive":
        from scenedetect.detectors import AdaptiveDetector
        detector = AdaptiveDetector()
        if "threshold" in detector_args:
            detector.adaptive_threshold = detector_args["threshold"]
            unused_args.pop("threshold", None)
        for key in ["frame_window", "min_content_val", "weights", "luma_only", "kernel_size", "min_scene_len"]:
            if key in detector_args:
                setattr(detector, key, detector_args[key])
                unused_args.pop(key, None)
    elif method == "detect-content":
        from scenedetect.detectors import ContentDetector
        ctor_keys = ["threshold", "weights", "luma_only", "kernel_size", "min_scene_len", "frame_window"]
        ctor_args = {k: detector_args[k] for k in ctor_keys if k in detector_args}
        detector = ContentDetector(**ctor_args)
        for k in ctor_args:
            unused_args.pop(k, None)
    elif method == "detect-hist":
        from scenedetect.detectors import HistogramDetector
        ctor_keys = ["threshold", "bins", "min_scene_len"]
        ctor_args = {k: detector_args[k] for k in ctor_keys if k in detector_args}
        detector = HistogramDetector(**ctor_args)
        for k in ctor_args:
            unused_args.pop(k, None)
    elif method == "detect-threshold":
        from scenedetect.detectors import ThresholdDetector
        ctor_keys = ["threshold", "fade_bias", "add_last_scene", "min_scene_len"]
        ctor_args = {k: detector_args[k] for k in ctor_keys if k in detector_args}
        detector = ThresholdDetector(**ctor_args)
        for k in ctor_args:
            unused_args.pop(k, None)
    else:
        from scenedetect.detectors import ContentDetector
        detector = ContentDetector()
    if unused_args:
        print(f"Warning: The following detector options were not used: {', '.join(unused_args.keys())}")
    # print("vars(detector):", vars(detector))
    return detector

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
        start_time = time.time()
        try:
            video = open_video(self.video_path)
            scene_manager = SceneManager()
            detector_args = parse_detector_args(self.weights)
            # print(f"Parsed detector args: {detector_args}")
            detector = create_detector(self.method, detector_args)
            scene_manager.add_detector(detector)
            scene_manager.detect_scenes(video)
            scene_list = scene_manager.get_scene_list()
        except Exception as e:
            scene_list = [f"Error: {e}"]
        elapsed = time.time() - start_time
        elapsed_str = time.strftime("%H:%M:%S", time.gmtime(elapsed))
        print(f"Scene detection finished. Elapsed time: {elapsed_str}")
        self.finished.emit(scene_list)

class DetectorWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    jump_to_timecode_signal = pyqtSignal(str, bool)
    shotlist_status = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self._pending_save_data = {}
        self.setWindowTitle("Shotlist")
        self.setGeometry(200, 200, 600, 400)
        self.detections_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scenes")
        os.makedirs(self.detections_folder, exist_ok=True)
        self.detecting_timer = QTimer()
        self.detecting_timer.timeout.connect(self.animate_detecting)
        self.detecting_dots = 0

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        self.scene_table = QTableWidget()
        self.scene_table.setColumnCount(5)
        self.scene_table.setHorizontalHeaderLabels(["Ignore", "Scene", "Start", "End", "Caption"])
        self.scene_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.scene_table.cellClicked.connect(self.on_table_cell_clicked)
        layout.addWidget(self.scene_table)
        self.scene_table.setColumnWidth(0, 110)   # Ignore
        self.scene_table.setColumnWidth(1, 80)    # Scene
        self.scene_table.setColumnWidth(2, 110)   # Start
        self.scene_table.setColumnWidth(3, 110)   # End
        self.scene_table.setColumnWidth(4, 500)   # Caption

        self.detect_button = QPushButton("Detect")
        self.detect_button.setFixedWidth(120)
        self.detect_button.setMinimumHeight(32)
        self.detect_button.setStyleSheet(
            "text-align: center; padding-left: 0px; padding-top: 3px; padding-bottom: 6px;"
        )
        self.detect_button.setEnabled(False)

        self.folder_button = QPushButton("Folder")
        self.folder_button.setFixedWidth(100)
        self.folder_button.setMinimumHeight(32)
        self.folder_button.clicked.connect(self.select_detections_folder)

        self.method_dropdown = QComboBox()
        self.method_dropdown.addItems([
            "detect-adaptive",
            "detect-content",
            "detect-hist",
            "detect-threshold"
        ])
        self.method_dropdown.setFixedWidth(150)

        self.weights_field = QLineEdit("-w 1.0 1.0 1.0 0.0 -t 32")
        self.weights_field.setFixedWidth(180)
        self.weights_field.setAlignment(Qt.AlignCenter)
        self.weights_field.setToolTip("Set PySceneDetect parameters.\nSee documentation for details.\nExamples:\nweights: -w 1.0 1.0 1.0 0.0\nthreshold: -t 3.2")

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

        self.video_path = None
        self.current_csv_path = None

        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)
        self.thread = None
        self.scene_table.itemChanged.connect(self.on_scene_table_item_changed)

    def select_detections_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Scene Detections Folder", self.detections_folder)
        if folder:
            self.detections_folder = folder

    def load_scene_detections(self, csv_path):
        with open(csv_path, "r", newline="") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        self.scene_table.setRowCount(0)
        for row in rows:
            ignore = row.get("Ignore", "No") == "Yes"
            scene_num = row.get("Scene", "")
            start = row.get("Start", "")
            end = row.get("End", "")
            caption = row.get("Caption", "")
            self.add_scene_row(scene_num, start, end, caption, ignore)
        self.current_csv_path = csv_path
        self.delete_button.setEnabled(True)

    def on_detect_scenes(self):
        self.shotlist_status.emit(False)
        self.detect_button.setEnabled(False)
        self.detecting_dots = 0
        self.detect_button.setText("Detecting")
        self.detect_button.setStyleSheet(
            "text-align: left; color: grey; padding-left: 30px; padding-top: 3px; padding-bottom: 6px;"
        )
        self.detecting_timer.start(500)
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
        method = self.method_dropdown.currentText()
        weights_text = self.weights_field.text().strip()

        # --- Write method and weights to .txt file ---
        base = os.path.basename(self.video_path)
        name, _ = os.path.splitext(base)
        txt_path = os.path.join(self.detections_folder, f"{name}.txt")
        with open(txt_path, "w") as txtfile:
            txtfile.write(f"{method}\n{weights_text}\n")

        # --- End .txt file writing ---

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
            self.scene_table.setRowCount(0)  # Clear all rows
            print("No scenes detected or an error occurred:", scene_list[0] if isinstance(scene_list, list) else scene_list)
            self.delete_button.setEnabled(False)
            # Notify AnnotateWindow to disable buttons
            self.shotlist_status.emit(False)
            return
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
            start_tc = scene[0].get_timecode()
            end_tc = scene[1].get_timecode()
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
            padded_start_ms = start_ms + start_padding
            padded_end_ms = max(end_ms - end_padding, 0)
            def ms_to_tc(ms):
                total_seconds = ms / 1000.0
                h = int(total_seconds // 3600)
                m = int((total_seconds % 3600) // 60)
                s = total_seconds % 60
                return f"{h:02}:{m:02}:{s:06.3f}"
            padded_start_tc = ms_to_tc(padded_start_ms)
            padded_end_tc = ms_to_tc(padded_end_ms)
            csv_rows.append(["No", 0, padded_start_tc, padded_end_tc, ""])
        if self.video_path:
            base = os.path.basename(self.video_path)
            name, _ = os.path.splitext(base)
            out_path = os.path.join(self.detections_folder, f"{name}.csv")
            with open(out_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Ignore", "Scene", "Start", "End", "Caption"])
                writer.writerows(csv_rows)
            self.current_csv_path = out_path
            self.delete_button.setEnabled(True)
            self.load_shotlist_from_csv(out_path)
            self.shotlist_status.emit(True)

    def on_table_cell_clicked(self, row, col):
        start_tc = self.scene_table.item(row, 2).text()  # Start is now col 2
        end_tc = self.scene_table.item(row, 3).text()    # End is now col 3
        if col == 2:
            self.jump_to_timecode(start_tc)
        elif col == 3:
            self.jump_to_timecode(end_tc, is_last_frame=True)

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
            "col3_width": self.scene_table.columnWidth(3),
            "col4_width": self.scene_table.columnWidth(4),
            "detections_folder": self.detections_folder,
            "weights_field": self.weights_field.text(),
            "method_selected": self.method_dropdown.currentText()
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
        if "col3_width" in data:
            self.scene_table.setColumnWidth(3, data["col3_width"])
        if "col4_width" in data:
            self.scene_table.setColumnWidth(4, data["col4_width"])
        if "detections_folder" in data:
            self.detections_folder = data["detections_folder"]
        if "weights_field" in data:
            self.weights_field.setText(data["weights_field"])
        if "method_selected" in data:
            idx = self.method_dropdown.findText(data["method_selected"])
            if idx != -1:
                self.method_dropdown.setCurrentIndex(idx)

    def process_video(self, video_path):
        shotlist_exists = False
        if video_path:
            self.video_path = video_path
            base = os.path.basename(video_path)
            name, _ = os.path.splitext(base)
            csv_path = os.path.join(self.detections_folder, f"{name}.csv")
            if os.path.exists(csv_path):
                self.load_scene_detections(csv_path)
                shotlist_exists = True
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
        # Emit shotlist status
        self.shotlist_status.emit(shotlist_exists)

    def delete_scene_csv(self):
        # Delete CSV file
        if self.current_csv_path and os.path.exists(self.current_csv_path):
            os.remove(self.current_csv_path)
            # Also delete the corresponding .txt file
            base = os.path.basename(self.current_csv_path)
            name, _ = os.path.splitext(base)
            txt_path = os.path.join(self.detections_folder, f"{name}.txt")
            if os.path.exists(txt_path):
                os.remove(txt_path)
        self.scene_table.setRowCount(0)
        self.current_csv_path = None
        self.delete_button.setEnabled(False)
        # Notify AnnotateWindow to disable buttons
        self.shotlist_status.emit(False)

    def clear_table_selection(self):
        self.scene_table.clearSelection()

    def handle_global_key(self, event):
        focus_widget = QApplication.focusWidget()
        if not isinstance(focus_widget, QTextEdit):
            self.keyPressEvent(event)

    def add_scene_row(self, scene_num, start_tc, end_tc, caption, ignore=False):
        row = self.scene_table.rowCount()
        self.scene_table.insertRow(row)
        # Ignore column (checkbox)
        checkbox = QCheckBox()
        checkbox.setChecked(ignore)
        checkbox.stateChanged.connect(lambda state, r=row: self.on_ignore_checkbox_changed(r, state))
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.addWidget(checkbox)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        self.scene_table.setCellWidget(row, 0, widget)
        # Scene number column
        scene_item = QTableWidgetItem(str(scene_num))
        scene_item.setTextAlignment(Qt.AlignCenter)
        self.scene_table.setItem(row, 1, scene_item)
        # Start column (fixed-width font)
        start_item = QTableWidgetItem(start_tc)
        start_item.setTextAlignment(Qt.AlignCenter)
        font = QFont("Courier New", 14)
        font.setBold(True)
        start_item.setFont(font)
        self.scene_table.setItem(row, 2, start_item)
        # End column (fixed-width font)
        end_item = QTableWidgetItem(end_tc)
        end_item.setTextAlignment(Qt.AlignCenter)
        font = QFont("Courier New", 14)
        font.setBold(True)
        end_item.setFont(font)
        self.scene_table.setItem(row, 3, end_item)
        # Caption column
        caption_item = QTableWidgetItem(caption)
        caption_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.scene_table.setItem(row, 4, caption_item)

    def on_ignore_checkbox_changed(self, row, state):
        self.save_shotlist_to_csv()

    def on_scene_table_item_changed(self, item):
        # Only needed if you allow editing other columns
        pass

    def save_shotlist_to_csv(self):
        if not self.current_csv_path:
            return
        with open(self.current_csv_path, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["Ignore", "Scene", "Start", "End", "Caption"])
            for row in range(self.scene_table.rowCount()):
                widget = self.scene_table.cellWidget(row, 0)
                checkbox = widget.findChild(QCheckBox)
                ignore = "Yes" if checkbox.isChecked() else "No"
                scene_num = self.scene_table.item(row, 1).text()
                start = self.scene_table.item(row, 2).text()
                end = self.scene_table.item(row, 3).text()
                caption = self.scene_table.item(row, 4).text()
                writer.writerow([ignore, scene_num, start, end, caption])

    def load_shotlist_from_csv(self, path):
        with open(path, "r") as csvfile:
            reader = csv.DictReader(csvfile)
            self.scene_table.setRowCount(0)
            for row in reader:
                ignore = row.get("Ignore", "No") == "Yes"
                scene_num = row.get("Scene", "")
                start = row.get("Start", "")
                end = row.get("End", "")
                caption = row.get("Caption", "")
                self.add_scene_row(scene_num, start, end, caption, ignore)