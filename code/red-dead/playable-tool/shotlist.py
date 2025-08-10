DEBUG = False  # Set to True to enable debug output

import csv
import os

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QBrush, QPalette
from PyQt5.QtWidgets import (
    QCheckBox, QHBoxLayout, QMainWindow,
    QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget
)

from scenedetect import open_video
from detector import ShotDetectWorker
from shotlist_worker import ShotlistLoadWorker
from utility import timecode_to_milliseconds, HIGHLIGHT_BACKGROUND_COLOR, HIGHLIGHT_COLOR

JUMP_FRAME_PADDING_PLAYBACK = 0  # Number of frames to pad when jumping in playback mode
JUMP_FRAME_PADDING_DETECTION = 5  # Number of frames to pad when jumping in detection mode

# --------------- SHOTLIST IMPORTER ---------------

class ShotlistImportWorker(QThread):
    finished = pyqtSignal()

    def __init__(self, project_folder, shotlist_db):
        super().__init__()
        self.project_folder = project_folder
        self.shotlist_db = shotlist_db

    def run(self):
        detections_folder = os.path.join(self.project_folder, "shotlists")
        os.makedirs(detections_folder, exist_ok=True)
        self.shotlist_db.clear()
        for fname in os.listdir(detections_folder):
            if fname.endswith(".csv"):
                base_name = os.path.splitext(fname)[0]
                csv_path = os.path.join(detections_folder, fname)
                try:
                    with open(csv_path, "r", encoding="utf-8") as csvfile:
                        reader = csv.DictReader(csvfile)
                        self.shotlist_db[base_name] = [row for row in reader]
                except Exception as e:
                    if DEBUG: print(f"DEBUG: Failed to load {csv_path}: {e}")
        self.finished.emit()

# --------------- SHOWTLIST WINDOW ---------------

class ShotlistWindow(QMainWindow):

    # define the signals we are going to send out
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
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
    is_first_available_shot = pyqtSignal(bool)  # Add this new signal

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

        # set the selection color to a custom color
        palette = self.scene_table.palette()
        palette.setColor(QPalette.Highlight, QColor(HIGHLIGHT_BACKGROUND_COLOR))
        palette.setColor(QPalette.HighlightedText, QColor(HIGHLIGHT_COLOR))
        self.scene_table.setPalette(palette)

        # Set column header font using UI 'collumn' style
        header_font = self.ui.get_font('collumn')
        header = self.scene_table.horizontalHeader()
        header.setFont(header_font)
        
        layout.addWidget(self.scene_table)
        self.scene_table.setColumnWidth(0, 110)   # Ignore
        self.scene_table.setColumnWidth(1, 80)    # Scene
        self.scene_table.setColumnWidth(2, 110)   # Start
        self.scene_table.setColumnWidth(3, 110)   # End
        self.scene_table.setColumnWidth(4, 300)   # Shot Caption
        self.scene_table.setColumnWidth(5, 300)   # Scene Caption
        
        self.scene_table.verticalHeader().setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.scene_table.setAlternatingRowColors(False)

        tiny_width, tiny_height = self.ui.get_dimensions('tiny')

        self.setCentralWidget(central_widget)

        self.video_path = None
        self.current_csv_path = None

        self.preferences_save.connect(self.on_preferences_save)
        self.preferences_load.connect(self.on_preferences_load)
        self.thread = None
        self.scene_table.itemChanged.connect(self.on_scene_table_item_changed)

        self.current_time_ms = 0
        self.current_row = -1  # Track current row
        self.last_current_row = -1  # Track previous row for comparison

        self.clear_selection_timer = QTimer(self)
        self.clear_selection_timer.setSingleShot(True)
        self.clear_selection_timer.timeout.connect(self.clear_table_selection)

        # Initialize shotlist database
        self.shotlist_db = {}  # Maps movie base name to list of shotlist rows

    def on_detect_scenes(self):
        self.shotlist_status.emit(False)
        self.scene_table.setRowCount(0)
        if not self.video_path or not os.path.exists(self.video_path):
            self.scene_table.setRowCount(1)
            return
        method = self.method_dropdown.currentText()
        weights_text = self.weights_field.text().strip()

        # --- Write method and weights to .txt file ---
        base = os.path.basename(self.video_path)
        name, _ = os.path.splitext(base)
        txt_path = os.path.join(self.detections_folder, f"{name}.txt")
        with open(txt_path, "w", encoding="utf-8") as txtfile:
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

    def on_detection_finished(self):
        self.detecting_timer.stop()

    def on_scene_detected(self, scene_list):
        self.on_detection_finished()
        if not scene_list or (isinstance(scene_list[0], str) and scene_list[0].startswith("Error:")):
            self.scene_table.setRowCount(0)  # Clear all rows
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
            start_ms = timecode_to_milliseconds(start_tc)
            end_ms = timecode_to_milliseconds(end_tc)
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
            with open(out_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"])
                writer.writerows(csv_rows)
            self.current_csv_path = out_path
            self.load_shotlist_from_csv(out_path)
            self.shotlist_status.emit(True)

    def on_row_header_clicked(self, row):
        """Handle clicking on row header (row number on the left)"""
        if DEBUG: print(f"DEBUG: Row header clicked: {row}")
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
            self.on_row_header_clicked(row) # changed from previous implementation
            # self.emit_shot_caption_for_row(row)
        else:
            pass

    def jump_to_timecode(self, timecode, is_last_frame=False):
        # send out signal to jump to this timecode
        self.jump_to_timecode_signal.emit(timecode, is_last_frame)

    # ------- Shotlist Bot -------

    def start_shotlist_bot(self):
        pass

    # ------- Load/Save Preferences -------

    def on_preferences_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "col0_width": self.scene_table.columnWidth(0),
            "col1_width": self.scene_table.columnWidth(1),
            "col2_width": self.scene_table.columnWidth(2),
            "col3_width": self.scene_table.columnWidth(3),
            "col4_width": self.scene_table.columnWidth(4),
            "col5_width": self.scene_table.columnWidth(5)
        }

    def on_preferences_load(self, data):
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
        if "col5_width" in data:
            self.scene_table.setColumnWidth(5, data["col5_width"])

    # ------- Video Processing -------

    def on_movie_loading(self):
        if DEBUG: print("DEBUG: Shotlist is loading video...")
        self.scene_table.setRowCount(0)
        self.current_csv_path = None

    def on_movie_loaded(self, video_path, metadata):
        # Prevent table creation or access if shotlist DB is not loaded
        if not self.shotlist_db_loaded:
            if DEBUG: print("DEBUG: Shotlist DB not loaded yet, skipping on_movie_loaded.")
            return
        # wait a second then load this
        delay = 1000
        QTimer.singleShot(delay, lambda: self.load_movie_shotlist_after_delay(video_path, metadata))

    def load_movie_shotlist_after_delay(self, video_path, metadata):

        shotlist_exists = False
        self.video_path = video_path
        if video_path:
            base = os.path.basename(video_path)
            name, _ = os.path.splitext(base)
            # Use preloaded shotlist if available
            if name in self.shotlist_db:
                shotlist_exists = True
                self.scene_table.setRowCount(0)
                for row in self.shotlist_db[name]:
                    ignore = row.get("Ignore", "No") == "Yes"
                    scene_num = row.get("Scene", "")
                    start = row.get("Start", "")
                    end = row.get("End", "")
                    shot_caption = row.get("Shot_Caption", "")
                    scene_caption = row.get("Scene_Caption", "")
                    self.add_scene_row(scene_num, start, end, shot_caption, scene_caption, ignore)
                self.current_csv_path = os.path.join(self.detections_folder, f"{name}.csv")
            else:
                self.scene_table.setRowCount(0)
                self.current_csv_path = None
        else:
            self.video_path = None
            self.scene_table.setRowCount(0)
            self.current_csv_path = None
            self.current_row = -1
            self.last_current_row = -1
            self.current_time_ms = 0

        self.shotlist_status.emit(shotlist_exists)
        if DEBUG: print(f"DEBUG: on_movie_loaded - shotlist_exists: {shotlist_exists}")
        self.send_row_data()

    def on_shotlist_loaded(self, rows):
        self.scene_table.setRowCount(0)
        for row in rows:
            ignore = row.get("Ignore", "No") == "Yes"
            scene_num = row.get("Scene", "")
            start = row.get("Start", "")
            end = row.get("End", "")
            shot_caption = row.get("Shot_Caption", "")
            scene_caption = row.get("Scene_Caption", "")
            self.add_scene_row(scene_num, start, end, shot_caption, scene_caption, ignore)
        self.shotlist_status.emit(True)
        self.send_row_data()

    def on_shotlist_load_error(self, error_msg):
        self.scene_table.setRowCount(0)
        self.shotlist_status.emit(False)
        # Optionally, show error to user
        if DEBUG:
            print(f"Shotlist load error: {error_msg}")

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
        self.shotlist_status.emit(False)

    def clear_table_selection(self):
        self.scene_table.clearSelection()

    def handle_global_key(self, event):
        """Handle global key events"""

        key = event.key()
        modifiers = event.modifiers()

        if key == Qt.Key_Up:

            if DEBUG: print(f"DEBUG: KeyUp pressed, modifiers: {modifiers}")
            if modifiers & Qt.ShiftModifier:
                pass
            self.jump_to_previous_shot()
            if DEBUG: print("DEBUG: Jumping to previous shot")
            pass

        elif key == Qt.Key_Down:
            if DEBUG: print(f"DEBUG: KeyDown pressed, modifiers: {modifiers}")
            if modifiers & Qt.ShiftModifier:
                pass
            self.jump_to_next_shot()
            if DEBUG: print("DEBUG: Jumping to next shot")
            pass
        else:
            super().keyPressEvent(event)

    # def handle_global_key(self, event):
    #     focus_widget = QApplication.focusWidget()
    #     if not isinstance(focus_widget, QTextEdit):
    #         self.keyPressEvent(event)

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
        with open(self.current_csv_path, "w", newline="", encoding="utf-8") as csvfile:
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

    def clear_project(self):
        """Clear project - for consistency with other windows"""
        # A Placeholder for future functionality
        if DEBUG: print("DEBUG: ProjectWindow: clear_project called (no action needed)")

    def load_shotlist_from_csv(self, path):
        with open(path, "r", encoding="utf-8") as csvfile:
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
        if DEBUG: print(f"DEBUG: Sending row data for row {row_index if row_index is not None else self.current_row}")
        # make sure row index is > -1
        if row_index is None:
            row_index = self.current_row
        
        if row_index < 0 or row_index >= self.scene_table.rowCount():
            if DEBUG: print("DEBUG: Invalid row index, cannot send data.")
            return
        
        row_data = self.get_column_data(row_index)
        self.row_data.emit(row_data)

    # timecode change from player
    def on_timecode_changed(self, timecode):
        # call the internal set_current_time method
        self.set_current_time(timecode)
        # clear any current interface selections
        self.clear_selection_timer.start(100)

    def set_current_time(self, ms):

        self.current_time_ms = ms
        row_count = self.scene_table.rowCount()

        # if we don't have any rows, we can't do anything
        if row_count == 0:
            self.current_row = -1
            self.is_last_available_shot.emit(True)
            self.is_first_available_shot.emit(True)  # Add this line
            return

        # Check if current row changed
        new_current_row = self.find_closest_row(ms)
        if new_current_row != self.current_row:
            self.current_row = new_current_row
            self.row_did_change.emit(self.current_row)

            # send out the current row data
            if DEBUG: print(f"DEBUG: set_current_time. current_row: {self.current_row}")
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

        # Update first shot status
        first_non_ignored = self.is_first_non_ignored_row(self.current_row)
        self.is_first_available_shot.emit(first_non_ignored)

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
                    index_item.setBackground(QColor(HIGHLIGHT_BACKGROUND_COLOR))
                    index_item.setForeground(QBrush(QColor(HIGHLIGHT_COLOR)))

    def is_first_non_ignored_row(self, current_row):
        """Check if there are any non-ignored shots before the current position"""
        row_count = self.scene_table.rowCount()
        
        if row_count == 0:
            return True
        
        # Start searching from current_row - 1, or from last row if current_row is invalid
        end_row = min(current_row - 1, row_count - 1) if current_row > 0 else -1
        
        # Check if there are any non-ignored rows before current position
        for row in range(end_row, -1, -1):
            widget = self.scene_table.cellWidget(row, 0)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    return False  # Found a non-ignored row before current
        
        return True  # No non-ignored rows found before current position

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
            
        start_ms = timecode_to_milliseconds(start_tc)
        end_ms = timecode_to_milliseconds(end_tc)
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
            if DEBUG: print(f"DEBUG: on_row_selected - current_row changed to {self.current_row}")
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
        
        # Get the start time of the first shot for comparison
        start_index = self.get_column_index_by_name("Start")
        if start_index == -1:
            return -1
        
        first_shot_start = timecode_to_milliseconds(self.scene_table.item(0, start_index).text())
        
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
            start_ms = timecode_to_milliseconds(start_tc)
            end_ms = timecode_to_milliseconds(end_tc)

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

        new_row = 0
        start_col = self.get_column_index_by_name("Start")
        if start_col == -1:
            return -1  # Start column not found

        # Iterate through rows to find the closest start time less than or equal
        for row in range(row_count):
            start_tc = self.scene_table.item(row, start_col).text()
            start_ms = timecode_to_milliseconds(start_tc)
            if start_ms is None:
                continue  # Skip invalid or empty timecodes
            if start_ms <= ms:
                new_row = row
            else:
                break

        return new_row
    
    def handle_detect_scenes(self):
        """Handle the Detect Scenes button press."""
        pass

    def on_project_folder_loaded(self, project_folder):
        """Set the project folder and update detections folder, preload all shotlists (threaded)."""
        self.project_folder = project_folder
        self.detections_folder = os.path.join(project_folder, "shotlists")
        self.project_folder = project_folder
        self.shotlist_db_loaded = False
        self.shotlist_db = {}  # Clear previous cache

        # Start worker thread to load shotlists
        self.worker = ShotlistImportWorker(project_folder, self.shotlist_db)
        self.worker.finished.connect(self.shotlist_finished_loading)
        self.worker.start()

    def shotlist_finished_loading(self):
        """Called when shotlist DB has finished loading"""
        self.shotlist_db_loaded = True
        if DEBUG: print("DEBUG: Shotlist DB finished loading.")
        # Optionally, update UI or emit a signal here

    def select_first_available_shot(self):
        """Select the first non-ignored shot and update current_row."""
        row_count = self.scene_table.rowCount()
        if DEBUG: print(f"DEBUG: select_first_available_shot called, row_count={row_count}")
        if row_count == 0:
            if DEBUG: print("DEBUG: No rows in table, setting current_row to -1")
            self.current_row = -1
            self.row_did_change.emit(self.current_row)
            self.send_row_data()
            self.is_first_available_shot.emit(True)
            self.is_last_available_shot.emit(True)
            return

        # Find first non-ignored row
        for row in range(row_count):
            ignore_col_index = self.get_column_index_by_name("Ignore")
            if DEBUG: print(f"DEBUG: Checking row {row}, ignore_col_index={ignore_col_index}")
            if ignore_col_index == -1:
                if DEBUG: print(f"DEBUG: Ignore column not found for row {row}, skipping")
                continue
            widget = self.scene_table.cellWidget(row, ignore_col_index)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox:
                    if DEBUG: print(f"DEBUG: Row {row} checkbox checked={checkbox.isChecked()}")
                    if not checkbox.isChecked():
                        # Found first non-ignored shot
                        if DEBUG: print(f"DEBUG: Selecting row {row} as first available shot")
                        self.current_row = row
                        self.scroll_to_row(self.current_row)
                        self.send_row_data(self.current_row)
                        self.row_did_change.emit(self.current_row)
                        # Update first/last available shot signals
                        self.is_first_available_shot.emit(self.is_first_non_ignored_row(self.current_row))
                        self.is_last_available_shot.emit(self.is_last_non_ignored_row(self.current_row))
                        return

        # If all shots are ignored
        if DEBUG: print("DEBUG: All shots are ignored, setting current_row to -1")
        self.current_row = -1
        self.row_did_change.emit(self.current_row)
        self.send_row_data()
        self.is_first_available_shot.emit(True)
        self.is_last_available_shot.emit(True)
