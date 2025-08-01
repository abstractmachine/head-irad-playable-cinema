DEBUG = False  # Set to True to enable debug output

import csv
import os
from re import S

from PyQt5.QtCore import pyqtSignal
from PyQt5.QtCore import Qt, QThread, QTimer
from PyQt5.QtGui import QColor, QBrush
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QMainWindow,
    QPushButton, QTableWidget, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget
)

from scenedetect import open_video
from detector import ShotDetectWorker

JUMP_FRAME_PADDING_PLAYBACK = 0  # Number of frames to pad when jumping in playback mode
JUMP_FRAME_PADDING_DETECTION = 5  # Number of frames to pad when jumping in detection mode

class ShotlistWindow(QMainWindow):

    # define the signals we are going to send out
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    jump_to_timecode_signal = pyqtSignal(str, bool)
    shotlist_status = pyqtSignal(bool)
    shot_timecodes = pyqtSignal(str, list)  # start_tc, timecodes
    abort_api = pyqtSignal(str)  # Optionally pass a message
    shot_caption_selected = pyqtSignal(str)  # Add this signal
    # Remove the old shot_position signal - bad architecture
    # shot_position = pyqtSignal(int, int)  # current_row, row_count
    row_did_change = pyqtSignal(int)  # New signal: emits current_row when it changes
    row_data = pyqtSignal(dict)
    is_last_available_shot = pyqtSignal(bool)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui  # Store UI instance

        self.is_dark_mode = self.ui.is_dark_mode()

        self._pending_save_data = {}
        self.setWindowTitle("Shotlist")
        self.setGeometry(200, 200, 600, 400)
        
        # Initialize detections_folder to None - will be set when project loads
        self.detections_folder = None
        self.project_folder = None

        self.detecting_timer = QTimer()
        self.detecting_timer.timeout.connect(self.animate_detecting)
        self.detecting_dots = 0

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)   # Set margins to 0
        layout.setSpacing(0)                    # Set spacing to 0

        self.scene_table = QTableWidget()
        self.scene_table.setColumnCount(6)
        self.scene_table.setHorizontalHeaderLabels([
            "Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"
        ])
        self.scene_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.scene_table.cellClicked.connect(self.on_table_cell_clicked)
        
        self.scene_table.verticalHeader().sectionClicked.connect(self.on_row_header_clicked)

        # Set column header font using UI 'collumn' style
        header_font = self.ui.get_font('collumn')
        header = self.scene_table.horizontalHeader()
        header.setFont(header_font)

        button_width, button_height = self.ui.get_dimensions('button')
        
        layout.addWidget(self.scene_table)
        self.scene_table.setColumnWidth(0, 110)   # Ignore
        self.scene_table.setColumnWidth(1, 80)    # Scene
        self.scene_table.setColumnWidth(2, 110)   # Start
        self.scene_table.setColumnWidth(3, 110)   # End
        self.scene_table.setColumnWidth(4, 300)   # Shot Caption
        self.scene_table.setColumnWidth(5, 300)   # Scene Caption
        
        self.scene_table.verticalHeader().setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.scene_table.setAlternatingRowColors(False)

        self.method_dropdown = QComboBox()
        self.method_dropdown.addItems([
            "detect-adaptive",
            "detect-content",
            "detect-hist",
            "detect-threshold"
        ])
        self.method_dropdown.setFixedSize(130, button_height)
        self.method_dropdown.setToolTip("Select the detection method.\nSee documentation for details.")
        self.method_dropdown.setCurrentIndex(0)
        self.method_dropdown.setFont(self.ui.get_font('button'))
        # push this button using margins
        self.method_dropdown.setContentsMargins(0, 0, 0, 0)

        tiny_width, tiny_height = self.ui.get_dimensions('tiny')

        self.weights_field = QLineEdit("-t 3.0")
        self.weights_field.setAlignment(Qt.AlignCenter)
        self.weights_field.setToolTip("Set PySceneDetect parameters.\nSee documentation for details.\nExamples:\nweights: -w 1.0 1.0 1.0 0.0\nthreshold: -t 3.2")
        self.weights_field.setFont(self.ui.get_font('tiny-condensed'))
        self.weights_field.setStyleSheet("QLineEdit { margin: 0px 10px 0px 15px; }")
        self.weights_field.setFixedSize(120, tiny_height)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setFixedSize(button_width, button_height)
        self.delete_button.setFont(self.ui.get_font('button'))
        self.delete_button.setEnabled(False)
        self.delete_button.clicked.connect(self.delete_scene_csv)

        self.detect_button = QPushButton("Detect Shots")
        self.detect_button.setFixedSize(120, button_height)
        self.detect_button.setFont(self.ui.get_font('button'))
        self.detect_button.setEnabled(False)

        # New Detect Scenes button
        self.detect_scenes_button = QPushButton("Detect Scenes")
        self.detect_scenes_button.setFixedSize(120, button_height)
        self.detect_scenes_button.setFont(self.ui.get_font('button'))
        self.detect_scenes_button.setEnabled(False)  # Inactive for now
        self.detect_scenes_button.clicked.connect(self.handle_detect_scenes)

        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to 0
        button_layout.setSpacing(0)                   # Set spacing to 0
        button_layout.addStretch()
        button_layout.addWidget(self.method_dropdown)
        button_layout.addWidget(self.weights_field)
        button_layout.addWidget(self.detect_button)
        button_layout.addWidget(self.detect_scenes_button)
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

        self.current_time_ms = 0
        self.current_row = -1  # Track current row
        self.last_current_row = -1  # Track previous row for comparison

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
            shot_caption = row.get("Shot_Caption", "")
            scene_caption = row.get("Scene_Caption", "")
            self.add_scene_row(scene_num, start, end, shot_caption, scene_caption, ignore)
        self.current_csv_path = csv_path
        self.delete_button.setEnabled(True)

    def on_detect_scenes(self):
        self.shotlist_status.emit(False)
        self.detect_button.setEnabled(False)
        self.detecting_dots = 0
        self.detect_button.setText("        Detecting")
        self.detect_button.setStyleSheet(
            "text-align: left;"
        )
        self.detecting_timer.start(500)
        self.scene_table.setRowCount(0)
        if not self.video_path or not os.path.exists(self.video_path):
            self.scene_table.setRowCount(1)
            self.detect_button.setEnabled(True)
            self.detect_button.setText("Detect Shots")
            self.detect_button.setStyleSheet(
                "text-align: center;"
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

        self.shot_worker = ShotDetectWorker(self.video_path, method, weights_text)
        self.thread = QThread()
        self.shot_worker.moveToThread(self.thread)
        self.thread.started.connect(self.shot_worker.run)
        self.shot_worker.finished.connect(self.on_scene_detected)
        self.shot_worker.finished.connect(self.thread.quit)
        self.shot_worker.finished.connect(self.shot_worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.finished.connect(self.on_detection_finished)
        self.thread.start()

    def animate_detecting(self):
        self.detecting_dots = (self.detecting_dots + 1) % 4
        self.detect_button.setText("        Detecting" + "." * self.detecting_dots)
        self.detect_button.setStyleSheet(
            "text-align: left;"
        )

    def on_detection_finished(self):
        self.detect_button.setEnabled(True)
        self.detect_button.setText("Detect Shots")
        self.detect_button.setStyleSheet(
            "text-align: center;"
        )
        self.detecting_timer.stop()

    def on_scene_detected(self, scene_list):
        self.on_detection_finished()
        if not scene_list or (isinstance(scene_list[0], str) and scene_list[0].startswith("Error:")):
            self.scene_table.setRowCount(0)  # Clear all rows
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
            csv_rows.append(["No", 0, padded_start_tc, padded_end_tc, "", ""])
        if self.video_path:
            base = os.path.basename(self.video_path)
            name, _ = os.path.splitext(base)
            out_path = os.path.join(self.detections_folder, f"{name}.csv")
            with open(out_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"])
                writer.writerows(csv_rows)
            self.current_csv_path = out_path
            self.delete_button.setEnabled(True)
            self.load_shotlist_from_csv(out_path)
            self.shotlist_status.emit(True)

    def on_row_header_clicked(self, row):
        """Handle clicking on row header (row number on the left)"""
        # Jump to the start of this shot
        self.jump_to_row_start(row)
        # Emit the caption for this shot
        self.emit_shot_caption_for_row(row)

    def on_table_cell_clicked(self, row, col):
        # Get the column header text to determine what was clicked
        header_item = self.scene_table.horizontalHeaderItem(col)
        if not header_item:
            return
        
        column_title = header_item.text()
        
        if column_title == "Start":
            start_tc = self.scene_table.item(row, col).text()
            self.jump_to_timecode(start_tc)
        elif column_title == "End":
            end_tc = self.scene_table.item(row, col).text()
            self.jump_to_timecode(end_tc, is_last_frame=True)
        elif column_title == "Shot_Caption":
            # Emit the caption for this shot
            self.emit_shot_caption_for_row(row)
        else:
            pass

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

    # ------- Shotlist Bot -------

    def start_shotlist_bot(self):
        # Only start if Detect Shots button is enabled (not already running)
        if self.detect_button.isEnabled():
            self.detect_button.click()
        else:
            # Already running, do nothing
            pass

    # ------- Load/Save Preferences -------

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "col0_width": self.scene_table.columnWidth(0),
            "col1_width": self.scene_table.columnWidth(1),
            "col2_width": self.scene_table.columnWidth(2),
            "col3_width": self.scene_table.columnWidth(3),
            "col4_width": self.scene_table.columnWidth(4),
            "weights_field": self.weights_field.text(),
            "method_selected": self.method_dropdown.currentText()
        }

    def on_request_load(self, data):
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
        if "weights_field" in data:
            self.weights_field.setText(data["weights_field"])
        if "method_selected" in data:
            idx = self.method_dropdown.findText(data["method_selected"])
            if idx != -1:
                self.method_dropdown.setCurrentIndex(idx)

    # ------- Video Processing -------

    def on_movie_loaded_with_metadata(self, video_path, metadata):
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
        # Emit data of the first row if it exists
        self.send_row_data()

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

    def add_scene_row(self, scene_num, start_tc, end_tc, shot_caption, scene_caption, ignore=False):
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
        ignore_col = self.get_column_index_by_name("Ignore")
        self.scene_table.setCellWidget(row, ignore_col, widget)

        # Scene number column
        scene_col = self.get_column_index_by_name("Scene")
        scene_item = QTableWidgetItem(str(scene_num))
        scene_item.setTextAlignment(Qt.AlignCenter)
        scene_item.setFont(self.ui.get_font('cell-tiny'))
        self.scene_table.setItem(row, scene_col, scene_item)

        # Start column
        start_col = self.get_column_index_by_name("Start")
        start_item = QTableWidgetItem(start_tc)
        start_item.setTextAlignment(Qt.AlignCenter)
        start_item.setFont(self.ui.get_font('cell-mono'))
        self.scene_table.setItem(row, start_col, start_item)

        # End column
        end_col = self.get_column_index_by_name("End")
        end_item = QTableWidgetItem(end_tc)
        end_item.setTextAlignment(Qt.AlignCenter)
        end_item.setFont(self.ui.get_font('cell-mono'))
        self.scene_table.setItem(row, end_col, end_item)

        # Shot Caption column
        shot_caption_col = self.get_column_index_by_name("Shot_Caption")
        shot_caption_item = QTableWidgetItem(shot_caption)
        shot_caption_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        shot_caption_item.setFont(self.ui.get_font('cell-text'))
        self.scene_table.setItem(row, shot_caption_col, shot_caption_item)

        # Scene Caption column
        scene_caption_col = self.get_column_index_by_name("Scene_Caption")
        scene_caption_item = QTableWidgetItem(scene_caption)
        scene_caption_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        scene_caption_item.setFont(self.ui.get_font('cell-text'))
        self.scene_table.setItem(row, scene_caption_col, scene_caption_item)

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
            writer.writerow(["Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"])
            for row in range(self.scene_table.rowCount()):
                widget = self.scene_table.cellWidget(row, 0)
                checkbox = widget.findChild(QCheckBox)
                ignore = "Yes" if checkbox.isChecked() else "No"
                scene_index = self.get_column_index_by_name("Scene")
                scene_num = self.scene_table.item(row, scene_index).text()
                start_index = self.get_column_index_by_name("Start")
                start = self.scene_table.item(row, start_index).text()
                end_index = self.get_column_index_by_name("End")
                end = self.scene_table.item(row, end_index).text()
                shot_caption_index = self.get_column_index_by_name("Shot_Caption")
                shot_caption = self.scene_table.item(row, shot_caption_index).text() if shot_caption_index != -1 else ""
                scene_caption_index = self.get_column_index_by_name("Scene_Caption")
                scene_caption = self.scene_table.item(row, scene_caption_index).text() if scene_caption_index != -1 else ""
                writer.writerow([ignore, scene_num, start, end, shot_caption, scene_caption])

    def load_shotlist_from_csv(self, path):
        with open(path, "r") as csvfile:
            reader = csv.DictReader(csvfile)
            self.scene_table.setRowCount(0)
            for row in reader:
                ignore = row.get("Ignore", "No") == "Yes"
                scene_num = row.get("Scene", "")
                start = row.get("Start", "")
                end = row.get("End", "")
                shot_caption = row.get("Shot_Caption", "")
                scene_caption = row.get("Scene_Caption", "")
                self.add_scene_row(scene_num, start, end, shot_caption, scene_caption, ignore)

    def update_shot_caption_for_current_shot(self, shot_caption_text):
        row = self.find_closest_row(self.current_time_ms)
        if row is not None:
            shot_caption_col = self.get_column_index_by_name("Shot_Caption")
            self.scene_table.item(row, shot_caption_col).setText(shot_caption_text)
            self.save_shotlist_to_csv()
        else:
            pass

    def get_column_data(self, row_index = None):
        """Get all data for a specific row as a dictionary."""

        if row_index == None:
            row_index = self.current_row  # Default to current row if not specified

        row_data = {}

        if (self.scene_table.rowCount() == 0 or row_index < 0 or row_index >= self.scene_table.rowCount()):
            return row_data  # Return empty dict if row index is invalid
        
        column_names = [self.scene_table.horizontalHeaderItem(i).text() for i in range(self.scene_table.columnCount())]
        for col_index, col_name in enumerate(column_names):
            item = self.scene_table.item(row_index, col_index)
            if item:
                row_data[col_name] = item.text()
            else:
                row_data[col_name] = ""

        # Send the row data back to whomever requested it
        return row_data

    def send_row_data(self, row_index = None):
        """Send the data of a specific row to the connected slot."""
        row_data = self.get_column_data(row_index)
        self.row_data.emit(row_data)

    def set_current_time(self, ms):
        self.current_time_ms = ms
        row_count = self.scene_table.rowCount()

        # if we don't have any rows, we can't do anything
        if row_count == 0:
            self.current_row = -1
            self.is_last_available_shot.emit(True)
            return

        # Check if current row changed
        new_current_row = self.find_closest_row(ms)
        if new_current_row != self.current_row:
            self.current_row = new_current_row
            self.row_did_change.emit(self.current_row)

            # send out the current row data
            self.send_row_data()

            # only emit shot caption when row changes - no table manipulation
            if self.current_row >= 0:
                # Scroll to the selected row
                self.scroll_to_row(self.current_row)
                # find column index of shot caption column
                shot_caption_col = self.get_column_index_by_name("Shot_Caption")
                if shot_caption_col != -1:
                    shot_caption = self.scene_table.item(self.current_row, shot_caption_col).text()
                    self.shot_caption_selected.emit(shot_caption)
            else:
                self.shot_caption_selected.emit("")

        # Update last shot status
        last_non_ignored = self.is_last_non_ignored_row(self.current_row)
        self.is_last_available_shot.emit(last_non_ignored)

        # Clear previous highlights
        for row in range(row_count):
            scene_col = self.get_column_index_by_name("Scene")
            if scene_col != -1:
                index_item = self.scene_table.item(row, scene_col)
                if index_item:
                    index_item.setBackground(Qt.transparent)
                    index_item.setForeground(QBrush(QColor("#000" if not self.is_dark_mode else "#fff")))

        # Highlight the current shot index cell
        if self.current_row >= 0:
            scene_col = self.get_column_index_by_name("Scene")
            if scene_col != -1:
                index_item = self.scene_table.item(self.current_row, scene_col)
                if index_item:
                    index_item.setBackground(QColor("#f0f"))
                    index_item.setForeground(QBrush(QColor("#fff")))

    def handle_request_current_shot(self, count):
        row = self.find_current_shot(self.current_time_ms)
        
        # Fix: Check for both None AND -1 (invalid row)
        if row is None or row < 0:
            self.abort_api.emit("No matching shot found for API request.")
            return
        
        # Continue with existing frame extraction logic...
        start_col = self.get_column_index_by_name("Start")
        end_col = self.get_column_index_by_name("End")
        
        if start_col == -1 or end_col == -1:
            self.abort_api.emit("Column structure error.")
            return
            
        # Check if the table items exist before accessing them
        start_item = self.scene_table.item(row, start_col)
        end_item = self.scene_table.item(row, end_col)
        
        if not start_item or not end_item:
            self.abort_api.emit("Shot data not available.")
            return
            
        start_tc = start_item.text()
        end_tc = end_item.text()
        
        if not start_tc or not end_tc:
            self.abort_api.emit("Invalid shot timecodes.")
            return
                
        # Generate timecodes for frame extraction
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
        total_steps = count + 2
        step_size = (end_ms - start_ms) / total_steps
        timecodes = []
        for i in range(1, total_steps - 1):
            ms = int(start_ms + i * step_size)
            h = ms // 3600000
            m = (ms % 3600000) // 60000
            s = ((ms % 60000) / 1000)
            tc = f"{h:02}:{m:02}:{s:06.3f}"
            timecodes.append(tc)
                
        # Emit the signal to player
        self.shot_timecodes.emit(start_tc, timecodes)

    def on_row_selected(self, selected, deselected):
        indexes = selected.indexes()
        if not indexes:
            return
        row = indexes[0].row()
        
        # Jump to Begin (start) timecode
        start_col = self.get_column_index_by_name("Start")
        start_tc = self.scene_table.item(row, start_col).text()
        self.jump_to_timecode(start_tc)

        shot_caption_index = self.get_column_index_by_name("Shot_Caption")

        # Block signals BEFORE calling setCurrentCell to avoid recursion
        self.scene_table.blockSignals(True)
        self.scene_table.setCurrentCell(row, shot_caption_index)
        self.scene_table.clearSelection()
        self.scene_table.blockSignals(False)
        
        # Emit the caption text to AnnotateWindow
        shot_caption = self.scene_table.item(row, shot_caption_index).text()
        self.caption_selected.emit(shot_caption)

        # Update current row and emit change signal
        if row != self.current_row:
            self.current_row = row
            self.row_did_change.emit(self.current_row)
            # Scroll to the selected row
            self.scroll_to_row(row)

    def scroll_to_row(self, row):
        if DEBUG: print(f"DEBUG: scroll_to_row called for row {row}")
        table = self.scene_table
        first_visible = table.rowAt(0)
        last_visible = table.rowAt(table.viewport().height() - 1)
        if last_visible == -1:
            last_visible = table.rowCount() - 1
        if row < first_visible or row > last_visible:
            if DEBUG: print(f"DEBUG: scrolling to row {row} (not visible: {first_visible}-{last_visible})")
            # Use the "Scene" column (usually index 1) for scrolling
            scene_col = self.get_column_index_by_name("Scene")
            if scene_col == -1:
                scene_col = 0  # fallback
            index = table.model().index(row, scene_col)
            table.scrollTo(index, table.PositionAtTop)
        else:
            if DEBUG: print(f"DEBUG: row {row} already visible ({first_visible}-{last_visible}), no scroll")

    def jump_to_next_shot(self):
        """Jump to the next non-ignored shot"""
        if DEBUG: print("DEBUG: jump_to_next_shot called")
        row_count = self.scene_table.rowCount()
        
        if row_count == 0:
            if DEBUG: print("DEBUG: No rows in table")
            return
        
        # Start searching from current_row + 1, or from 0 if current_row is invalid
        start_row = max(0, self.current_row + 1)
        if DEBUG: print(f"DEBUG: Searching for next shot starting from row {start_row}")
        
        # Find next non-ignored row
        for next_row in range(start_row, row_count):
            ignore_col_index = self.get_column_index_by_name("Ignore")
            if ignore_col_index == -1:
                continue
            
            widget = self.scene_table.cellWidget(next_row, ignore_col_index)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    # Found next non-ignored shot
                    if DEBUG: print(f"DEBUG: Found next non-ignored shot at row {next_row}")
                    start_col_index = self.get_column_index_by_name("Start")
                    start_tc = self.scene_table.item(next_row, start_col_index).text()
                    self.jump_to_timecode(start_tc)
                    return
        
        # No next shot found
        if DEBUG: print("DEBUG: Already at last available shot")

    def jump_to_previous_shot(self):
        """Jump to the previous non-ignored shot"""
        if DEBUG: print("DEBUG: jump_to_previous_shot called")
        row_count = self.scene_table.rowCount()
        
        if row_count == 0:
            if DEBUG: print("DEBUG: No rows in table")
            return
        
        # Start searching from current_row - 1, or from last row if current_row is invalid
        start_row = min(row_count - 1, self.current_row - 1 if self.current_row > 0 else -1)
        if DEBUG: print(f"DEBUG: Searching for previous shot starting from row {start_row}")
        
        # Find previous non-ignored row
        for prev_row in range(start_row, -1, -1):
            ignore_col_index = self.get_column_index_by_name("Ignore")
            if ignore_col_index == -1:
                continue
            
            widget = self.scene_table.cellWidget(prev_row, ignore_col_index)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    # Found previous non-ignored shot
                    if DEBUG: print(f"DEBUG: Found previous non-ignored shot at row {prev_row}")
                    start_col_index = self.get_column_index_by_name("Start")
                    start_tc = self.scene_table.item(prev_row, start_col_index).text()
                    self.jump_to_timecode(start_tc)
                    return
        
        # No previous shot found
        if DEBUG: print("DEBUG: Already at first available shot")

    def find_current_shot(self, ms):
        """Find the row index of the shot that contains the given time in ms"""
        row_count = self.scene_table.rowCount()
        
        if row_count == 0:
            return -1
        
        def tc_to_ms(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                return int((h * 3600 + m * 60 + s) * 1000)
            return 0
        
        # Get the start time of the first shot for comparison
        start_index = self.get_column_index_by_name("Start")
        if start_index == -1:
            return -1
        
        first_shot_start = tc_to_ms(self.scene_table.item(0, start_index).text())
        
        # If we're before the first shot, return the first shot
        if ms < first_shot_start:
            return 0
        
        # Otherwise, search for the shot that contains this time
        for row in range(row_count):
            end_index = self.get_column_index_by_name("End")
            if end_index == -1:
                return -1

            start_tc = self.scene_table.item(row, start_index).text()
            end_tc = self.scene_table.item(row, end_index).text()
            start_ms = tc_to_ms(start_tc)
            end_ms = tc_to_ms(end_tc)
            
            if start_ms <= ms < end_ms:
                return row
        
        # If we get here, we're after the last shot
        return -1

    def is_last_non_ignored_row(self, current_row):
        """Check if there are any non-ignored shots after the current position"""
        row_count = self.scene_table.rowCount()
        
        if row_count == 0:
            return True
        
        # Start searching from current_row + 1, or from 0 if current_row is invalid
        start_row = max(0, current_row + 1)
        
        # Check if there are any non-ignored rows after current position
        for row in range(start_row, row_count):
            widget = self.scene_table.cellWidget(row, 0)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    return False  # Found a non-ignored row after current
        
        return True  # No non-ignored rows found after current position

    def jump_to_row_start(self, row):
        """Jump to the specified row in the scene table"""
        start_index = self.get_column_index_by_name("Start")
        start_tc = self.scene_table.item(row, start_index).text()
        self.jump_to_timecode(start_tc)

    def emit_shot_caption_for_row(self, row):
        """emit the shot caption for this shot via signal"""
        shot_caption_index = self.get_column_index_by_name("Shot_Caption")
        shot_caption = self.scene_table.item(row, shot_caption_index).text()
        self.shot_caption_selected.emit(shot_caption)

    def get_column_index_by_name(self, column_name):
        """Find the column index by header name"""
        for col in range(self.scene_table.columnCount()):
            header_item = self.scene_table.horizontalHeaderItem(col)
            if header_item and header_item.text() == column_name:
                return col
        return -1  # Column not found

    def find_closest_row(self, ms):
        """Find the row index closest to the given time in ms"""
        row_count = self.scene_table.rowCount()
        
        if row_count == 0:
            return -1
        
        def tc_to_ms(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                return int((h * 3600 + m * 60 + s) * 1000)
            return 0
        
        # Start with the first row as candidate
        new_row = 0
        
        # Get the Start column index by name
        start_col = self.get_column_index_by_name("Start")
        if start_col == -1:
            return -1  # Start column not found
        
        for row in range(row_count):
            # Get the Begin (Start) time for this row
            start_tc = self.scene_table.item(row, start_col).text()
            start_ms = tc_to_ms(start_tc)
            
            # If this Begin time is equal to or before our current ms
            if start_ms <= ms:
                new_row = row
            else:
                # Begin time is after our ms, so we found our row
                break
        
        return new_row
    
    def handle_detect_scenes(self):
        print("Detect Scenes button pressed (dummy method).")

    def set_project_folder(self, project_folder):
        """Set the project folder and update detections folder"""
        self.project_folder = project_folder
        self.detections_folder = os.path.join(project_folder, "shotlists")
        os.makedirs(self.detections_folder, exist_ok=True)