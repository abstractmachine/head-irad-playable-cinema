import csv
import os

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QBrush, QPalette
from PyQt5.QtWidgets import (
    QCheckBox, QHBoxLayout, QMainWindow,
    QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget
)

from utility import timecode_to_milliseconds, HIGHLIGHT_BACKGROUND_COLOR, HIGHLIGHT_COLOR

LOADING_DELAY = 1000

class AbstractListImportWorker(QThread):
    finished = pyqtSignal()

    def __init__(self, project_folder, db, subfolder="shotlists", extension=".csv"):
        super().__init__()
        self.project_folder = project_folder
        self.db = db
        self.subfolder = subfolder
        self.extension = extension

    def run(self):
        detections_folder = os.path.join(self.project_folder, self.subfolder)
        os.makedirs(detections_folder, exist_ok=True)
        self.db.clear()
        for fname in os.listdir(detections_folder):
            # Ignore hidden/system files
            if fname.startswith("."):
                continue
            if fname.endswith(self.extension):
                base_name = os.path.splitext(fname)[0]
                file_path = os.path.join(detections_folder, fname)
                try:
                    with open(file_path, "r", encoding="utf-8") as csvfile:
                        reader = csv.DictReader(csvfile)
                        self.db[base_name] = [row for row in reader]
                except Exception as e:
                    print(f"DEBUG: Failed to load {file_path}: {e}")
        self.finished.emit()

class AbstractListWindow(QMainWindow):
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    jump_to_timecode_signal = pyqtSignal(str, bool)
    list_status = pyqtSignal(bool)
    timecodes_signal = pyqtSignal(str, list)
    abort_api = pyqtSignal(str)
    shot_caption_selected = pyqtSignal(str)
    scene_caption_selected = pyqtSignal(str)
    row_did_change = pyqtSignal(int)
    row_data = pyqtSignal(dict)
    is_last_available_row = pyqtSignal(bool)
    is_first_available_row = pyqtSignal(bool)

    COLUMN_HEADERS = [
        "Ignore", "Scene", "Start", "End", "Shot_Caption", "Scene_Caption"
    ]

    def __init__(self, ui, subfolder="shotlists"):
        super().__init__()
        self.ui = ui
        self.is_dark_mode = self.ui.is_dark_mode()
        self._pending_save_data = {}
        self.setWindowTitle("List")
        self.setGeometry(200, 200, 600, 400)
        self.detections_folder = None
        self.project_folder = None
        self.db_loaded = False
        self.basename = None
        self.video_path = None
        self.current_csv_path = None
        self.thread = None
        self.current_time_ms = 0
        self.current_row = -1
        self.last_current_row = -1
        self.clear_selection_timer = QTimer(self)
        self.clear_selection_timer.setSingleShot(True)
        self.clear_selection_timer.timeout.connect(self.clear_table_selection)
        self.db = {}
        self.column_indices = {}
        self.subfolder = subfolder

        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLUMN_HEADERS))
        self.table.setHorizontalHeaderLabels(self.COLUMN_HEADERS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.cellClicked.connect(self.on_table_cell_clicked)
        self.table.verticalHeader().sectionClicked.connect(self.on_row_header_clicked)
        palette = self.table.palette()
        palette.setColor(QPalette.Highlight, QColor(HIGHLIGHT_BACKGROUND_COLOR))
        palette.setColor(QPalette.HighlightedText, QColor(HIGHLIGHT_COLOR))
        self.table.setPalette(palette)
        header_font = self.ui.get_font('collumn')
        header = self.table.horizontalHeader()
        header.setFont(header_font)
        layout.addWidget(self.table)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 80)
        self.table.setColumnWidth(2, 110)
        self.table.setColumnWidth(3, 110)
        self.table.setColumnWidth(4, 300)
        self.table.setColumnWidth(5, 300)
        self.table.verticalHeader().setDefaultAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setAlternatingRowColors(False)
        self.setCentralWidget(central_widget)

        self.preferences_save.connect(self.on_preferences_save)
        self.preferences_load.connect(self.on_preferences_load)

    def on_row_header_clicked(self, row):
        self.jump_to_row_start(row)
        self.emit_caption_for_row(row)

    def on_table_cell_clicked(self, row, col):
        header_item = self.table.horizontalHeaderItem(col)
        if not header_item:
            return
        column_title = header_item.text()
        if column_title == "Start":
            start_tc = self.table.item(row, col).text()
            self.jump_to_timecode(start_tc)
        elif column_title == "End":
            end_tc = self.table.item(row, col).text()
            self.jump_to_timecode(end_tc, is_last_frame=True)
        elif column_title == "Shot_Caption":
            self.on_row_header_clicked(row)
        else:
            pass

    def jump_to_timecode(self, timecode, is_last_frame=False):
        self.jump_to_timecode_signal.emit(timecode, is_last_frame)

    def on_preferences_save(self):
        self._pending_save_data = {
            "col0_width": self.table.columnWidth(0),
            "col1_width": self.table.columnWidth(1),
            "col2_width": self.table.columnWidth(2),
            "col3_width": self.table.columnWidth(3),
            "col4_width": self.table.columnWidth(4),
            "col5_width": self.table.columnWidth(5)
        }

    def on_preferences_load(self, data):
        for i in range(len(self.COLUMN_HEADERS)):
            key = f"col{i}_width"
            if key in data:
                self.table.setColumnWidth(i, data[key])

    def on_movie_loading(self):
        self.table.setRowCount(0)
        self.current_csv_path = None

    def on_movie_loaded(self, video_path=None, metadata=None, delay=LOADING_DELAY):
        if not self.db_loaded:
            return
        QTimer.singleShot(delay, lambda: self.load_movie_list_after_delay(video_path, metadata))

    def load_movie_list_after_delay(self, video_path, metadata):
        list_exists = False
        self.video_path = video_path
        if video_path:
            base = os.path.basename(video_path)
            name, _ = os.path.splitext(base)
            self.basename = name
            if name in self.db:
                list_exists = True
                self.table.setRowCount(0)
                for row in self.db[name]:
                    ignore = row.get("Ignore", "No") == "Yes"
                    scene_num = row.get("Scene", "")
                    start = row.get("Start", "")
                    end = row.get("End", "")
                    shot_caption = row.get("Shot_Caption", "")
                    scene_caption = row.get("Scene_Caption", "")
                    self.add_row(scene_num, start, end, shot_caption, scene_caption, ignore)
                self.current_csv_path = os.path.join(self.detections_folder, f"{name}.csv")
            else:
                self.table.setRowCount(0)
                self.current_csv_path = None
        else:
            self.video_path = None
            self.basename = None
            self.table.setRowCount(0)
            self.current_csv_path = None
            self.current_row = -1
            self.last_current_row = -1
            self.current_time_ms = 0

        self.list_status.emit(list_exists)
        self.send_row_data()

    def on_list_loaded(self, rows):
        self.table.setRowCount(0)
        for row in rows:
            ignore = row.get("Ignore", "No") == "Yes"
            scene_num = row.get("Scene", "")
            start = row.get("Start", "")
            end = row.get("End", "")
            shot_caption = row.get("Shot_Caption", "")
            scene_caption = row.get("Scene_Caption", "")
            self.add_row(scene_num, start, end, shot_caption, scene_caption, ignore)
        self.list_status.emit(True)
        self.send_row_data()

    def on_list_load_error(self, error_msg):
        self.table.setRowCount(0)
        self.list_status.emit(False)
        print(f"List load error: {error_msg}")

    def clear_table_selection(self):
        self.table.clearSelection()

    def handle_global_key(self, event):
        key = event.key()
        modifiers = event.modifiers()
        if key == Qt.Key_Up:
            if modifiers & Qt.ShiftModifier:
                pass
            self.jump_to_previous_row()
        elif key == Qt.Key_Down:
            if modifiers & Qt.ShiftModifier:
                pass
            self.jump_to_next_row()
        else:
            super().keyPressEvent(event)

    def cache_column_indices(self):
        self.column_indices = {}
        for col in range(self.table.columnCount()):
            header_item = self.table.horizontalHeaderItem(col)
            if header_item:
                self.column_indices[header_item.text()] = col

    def get_column_index_by_name(self, column_name):
        return self.column_indices.get(column_name, -1)

    def add_row(self, scene_num, start_tc, end_tc, shot_caption, scene_caption, ignore=False):
        if not self.column_indices:
            self.cache_column_indices()
        row = self.table.rowCount()
        self.table.insertRow(row)
        ignore_col = self.get_column_index_by_name("Ignore")
        checkbox = QCheckBox()
        checkbox.setChecked(ignore)
        checkbox.stateChanged.connect(lambda state, r=row: self.on_ignore_checkbox_changed(r, state))
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.addWidget(checkbox)
        layout.setAlignment(Qt.AlignCenter)
        layout.setContentsMargins(0, 0, 0, 0)
        widget.setLayout(layout)
        self.table.setCellWidget(row, ignore_col, widget)
        scene_col = self.get_column_index_by_name("Scene")
        scene_item = QTableWidgetItem(str(scene_num))
        scene_item.setTextAlignment(Qt.AlignCenter)
        scene_item.setFont(self.ui.get_font('cell-tiny'))
        self.table.setItem(row, scene_col, scene_item)
        start_col = self.get_column_index_by_name("Start")
        start_item = QTableWidgetItem(start_tc)
        start_item.setTextAlignment(Qt.AlignCenter)
        start_item.setFont(self.ui.get_font('cell-mono'))
        self.table.setItem(row, start_col, start_item)
        end_col = self.get_column_index_by_name("End")
        end_item = QTableWidgetItem(end_tc)
        end_item.setTextAlignment(Qt.AlignCenter)
        end_item.setFont(self.ui.get_font('cell-mono'))
        self.table.setItem(row, end_col, end_item)
        shot_caption_col = self.get_column_index_by_name("Shot_Caption")
        shot_caption_item = QTableWidgetItem(shot_caption)
        shot_caption_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        shot_caption_item.setFont(self.ui.get_font('cell-text'))
        self.table.setItem(row, shot_caption_col, shot_caption_item)
        scene_caption_col = self.get_column_index_by_name("Scene_Caption")
        scene_caption_item = QTableWidgetItem(scene_caption)
        scene_caption_item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        scene_caption_item.setFont(self.ui.get_font('cell-text'))
        self.table.setItem(row, scene_caption_col, scene_caption_item)

    def on_ignore_checkbox_changed(self, row, state):
        self.update_db_row(row)
        self.save_list_to_csv()

    def clear_project(self):
        pass

    def update_caption_for_current_row(self, caption_text, column_name="Shot_Caption"):
        row = self.find_closest_row(self.current_time_ms)
        if row is not None:
            caption_col = self.get_column_index_by_name(column_name)
            item = self.table.item(row, caption_col)
            if item:
                item.setText(caption_text)
                self.update_db_row(row)
                self.save_list_to_csv()

    def save_list_to_csv(self):
        if not self.current_csv_path:
            return
        row_count = self.table.rowCount()
        col_count = self.table.columnCount()
        headers = [self.table.horizontalHeaderItem(i).text() for i in range(col_count)]
        with open(self.current_csv_path, "w", newline="", encoding="utf-8") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(headers)
            for row in range(row_count):
                row_data = []
                for col in range(col_count):
                    if headers[col] == "Ignore":
                        widget = self.table.cellWidget(row, col)
                        checkbox = widget.findChild(QCheckBox) if widget else None
                        row_data.append("Yes" if checkbox and checkbox.isChecked() else "No")
                    else:
                        item = self.table.item(row, col)
                        row_data.append(item.text() if item else "")
                writer.writerow(row_data)

    def get_column_data(self, row_index=None):
        if row_index is None:
            row_index = self.current_row
        row_data = {}
        if (self.table.rowCount() == 0 or row_index < 0 or row_index >= self.table.rowCount()):
            return row_data
        column_names = [self.table.horizontalHeaderItem(i).text() for i in range(self.table.columnCount())]
        for col_index, col_name in enumerate(column_names):
            item = self.table.item(row_index, col_index)
            row_data[col_name] = item.text() if item else ""
        return row_data

    def send_row_data(self, row_index=None):
        if row_index is None:
            row_index = self.current_row
        if row_index < 0 or row_index >= self.table.rowCount():
            return
        row_data = self.get_column_data(row_index)
        self.row_data.emit(row_data)

    def on_timecode_changed(self, timecode):
        self.set_current_time(timecode)
        self.clear_selection_timer.start(100)

    def set_current_time(self, ms):
        self.current_time_ms = ms
        row_count = self.table.rowCount()
        if row_count == 0:
            self.current_row = -1
            self.is_last_available_row.emit(True)
            self.is_first_available_row.emit(True)
            return
        new_current_row = self.find_closest_row(ms)
        if new_current_row != self.current_row:
            self.current_row = new_current_row
            self.row_did_change.emit(self.current_row)
            self.send_row_data()
            if self.current_row >= 0:
                self.scroll_to_row(self.current_row)
                caption_col = self.get_column_index_by_name("Shot_Caption")
                if caption_col != -1:
                    caption = self.table.item(self.current_row, caption_col).text()
                    self.shot_caption_selected.emit(caption)
            else:
                self.shot_caption_selected.emit("")
        last_non_ignored = self.is_last_non_ignored_row(self.current_row)
        self.is_last_available_row.emit(last_non_ignored)
        first_non_ignored = self.is_first_non_ignored_row(self.current_row)
        self.is_first_available_row.emit(first_non_ignored)
        for row in range(row_count):
            scene_col = self.get_column_index_by_name("Scene")
            if scene_col != -1:
                index_item = self.table.item(row, scene_col)
                if index_item:
                    index_item.setBackground(Qt.transparent)
                    index_item.setForeground(QBrush(QColor("#000" if not self.is_dark_mode else "#fff")))
        if self.current_row >= 0:
            scene_col = self.get_column_index_by_name("Scene")
            if scene_col != -1:
                index_item = self.table.item(self.current_row, scene_col)
                if index_item:
                    index_item.setBackground(QColor(HIGHLIGHT_BACKGROUND_COLOR))
                    index_item.setForeground(QBrush(QColor(HIGHLIGHT_COLOR)))

    def is_first_non_ignored_row(self, current_row):
        row_count = self.table.rowCount()
        if row_count == 0:
            return True
        end_row = min(current_row - 1, row_count - 1) if current_row > 0 else -1
        for row in range(end_row, -1, -1):
            widget = self.table.cellWidget(row, 0)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    return False
        return True

    def handle_request_current_row(self, count):
        row = self.find_current_row(self.current_time_ms)
        if row is None or row < 0:
            self.abort_api.emit("No matching row found for API request.")
            return
        start_col = self.get_column_index_by_name("Start")
        end_col = self.get_column_index_by_name("End")
        if start_col == -1 or end_col == -1:
            self.abort_api.emit("Column structure error.")
            return
        start_item = self.table.item(row, start_col)
        end_item = self.table.item(row, end_col)
        if not start_item or not end_item:
            self.abort_api.emit("Row data not available.")
            return
        start_tc = start_item.text()
        end_tc = end_item.text()
        if not start_tc or not end_tc:
            self.abort_api.emit("Invalid row timecodes.")
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
        self.timecodes_signal.emit(start_tc, timecodes)

    def on_row_selected(self, selected, deselected):
        indexes = selected.indexes()
        if not indexes:
            return
        row = indexes[0].row()
        start_col = self.get_column_index_by_name("Start")
        start_tc = self.table.item(row, start_col).text()
        self.jump_to_timecode(start_tc)
        caption_index = self.get_column_index_by_name("Shot_Caption")
        self.table.blockSignals(True)
        self.table.setCurrentCell(row, caption_index)
        self.table.clearSelection()
        self.table.blockSignals(False)
        caption = self.table.item(row, caption_index).text()
        self.shot_caption_selected.emit(caption)
        if row != self.current_row:
            self.current_row = row
            self.row_did_change.emit(self.current_row)
            self.scroll_to_row(row)

    def scroll_to_row(self, row):
        table = self.table
        first_visible = table.rowAt(0)
        last_visible = table.rowAt(table.viewport().height() - 1)
        if last_visible == -1:
            last_visible = table.rowCount() - 1
        if row < first_visible or row > last_visible:
            scene_col = self.get_column_index_by_name("Scene")
            if scene_col == -1:
                scene_col = 0
            index = table.model().index(row, scene_col)
            table.scrollTo(index, table.PositionAtTop)

    def jump_to_next_row(self):
        row_count = self.table.rowCount()
        if row_count == 0:
            return
        start_row = max(0, self.current_row + 1)
        for next_row in range(start_row, row_count):
            ignore_col_index = self.get_column_index_by_name("Ignore")
            if ignore_col_index == -1:
                continue
            widget = self.table.cellWidget(next_row, ignore_col_index)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    start_col_index = self.get_column_index_by_name("Start")
                    start_tc = self.table.item(next_row, start_col_index).text()
                    self.jump_to_timecode(start_tc)
                    return

    def jump_to_previous_row(self):
        row_count = self.table.rowCount()
        if row_count == 0:
            return
        start_row = min(row_count - 1, self.current_row - 1 if self.current_row > 0 else -1)
        for prev_row in range(start_row, -1, -1):
            ignore_col_index = self.get_column_index_by_name("Ignore")
            if ignore_col_index == -1:
                continue
            widget = self.table.cellWidget(prev_row, ignore_col_index)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    start_col_index = self.get_column_index_by_name("Start")
                    start_tc = self.table.item(prev_row, start_col_index).text()
                    self.jump_to_timecode(start_tc)
                    return

    def find_current_row(self, ms):
        row_count = self.table.rowCount()
        if row_count == 0:
            return -1
        start_index = self.get_column_index_by_name("Start")
        if start_index == -1:
            return -1
        first_row_start = timecode_to_milliseconds(self.table.item(0, start_index).text())
        if ms < first_row_start:
            return 0
        for row in range(row_count):
            end_index = self.get_column_index_by_name("End")
            if end_index == -1:
                return -1
            start_tc = self.table.item(row, start_index).text()
            end_tc = self.table.item(row, end_index).text()
            start_ms = timecode_to_milliseconds(start_tc)
            end_ms = timecode_to_milliseconds(end_tc)
            if start_ms <= ms < end_ms:
                return row
        return -1

    def is_last_non_ignored_row(self, current_row):
        row_count = self.table.rowCount()
        if row_count == 0:
            return True
        start_row = max(0, current_row + 1)
        for row in range(start_row, row_count):
            widget = self.table.cellWidget(row, 0)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    return False
        return True

    def jump_to_row_start(self, row):
        start_index = self.get_column_index_by_name("Start")
        start_tc = self.table.item(row, start_index).text()
        self.jump_to_timecode(start_tc)

    def emit_caption_for_row(self, row):
        caption_index = self.get_column_index_by_name("Shot_Caption")
        caption = self.table.item(row, caption_index).text()
        self.shot_caption_selected.emit(caption)

    def find_closest_row(self, ms):
        row_count = self.table.rowCount()
        if row_count == 0:
            return -1
        new_row = 0
        start_col = self.get_column_index_by_name("Start")
        if start_col == -1:
            return -1
        for row in range(row_count):
            start_tc = self.table.item(row, start_col).text()
            start_ms = timecode_to_milliseconds(start_tc)
            if start_ms is None:
                continue
            if start_ms <= ms:
                new_row = row
            else:
                break
        return new_row

    def on_project_folder_loaded(self, project_folder):
        self.project_folder = project_folder
        self.detections_folder = os.path.join(project_folder, self.subfolder)
        self.db_loaded = False
        self.db = {}
        self.worker = AbstractListImportWorker(
            project_folder, self.db, subfolder=self.subfolder, extension=".csv"
        )
        self.worker.finished.connect(self.list_finished_loading)
        self.worker.start()

    def list_finished_loading(self):
        self.db_loaded = True

    def select_first_available_row(self):
        row_count = self.table.rowCount()
        if row_count == 0:
            self.current_row = -1
            self.row_did_change.emit(self.current_row)
            self.send_row_data()
            self.is_first_available_row.emit(True)
            self.is_last_available_row.emit(True)
            return
        for row in range(row_count):
            ignore_col_index = self.get_column_index_by_name("Ignore")
            if ignore_col_index == -1:
                continue
            widget = self.table.cellWidget(row, ignore_col_index)
            if widget:
                checkbox = widget.findChild(QCheckBox)
                if checkbox and not checkbox.isChecked():
                    self.current_row = row
                    self.scroll_to_row(self.current_row)
                    self.send_row_data(self.current_row)
                    self.row_did_change.emit(self.current_row)
                    self.is_first_available_row.emit(self.is_first_non_ignored_row(self.current_row))
                    self.is_last_available_row.emit(self.is_last_non_ignored_row(self.current_row))
                    return
        self.current_row = -1
        self.row_did_change.emit(self.current_row)
        self.send_row_data()
        self.is_first_available_row.emit(True)
        self.is_last_available_row.emit(True)

    def update_db_row(self, row):
        if not self.basename:
            return
        name = self.basename
        if name not in self.db:
            return
        if row < 0 or row >= len(self.db[name]):
            return
        ignore_col = self.get_column_index_by_name("Ignore")
        scene_col = self.get_column_index_by_name("Scene")
        start_col = self.get_column_index_by_name("Start")
        end_col = self.get_column_index_by_name("End")
        shot_caption_col = self.get_column_index_by_name("Shot_Caption")
        scene_caption_col = self.get_column_index_by_name("Scene_Caption")
        widget = self.table.cellWidget(row, ignore_col)
        checkbox = widget.findChild(QCheckBox) if widget else None
        ignore_val = "Yes" if checkbox and checkbox.isChecked() else "No"
        self.db[name][row]["Ignore"] = ignore_val
        self.db[name][row]["Scene"] = self.table.item(row, scene_col).text()
        self.db[name][row]["Start"] = self.table.item(row, start_col).text()
        self.db[name][row]["End"] = self.table.item(row, end_col).text()
        self.db[name][row]["Shot_Caption"] = self.table.item(row, shot_caption_col).text()
        self.db[name][row]["Scene_Caption"] = self.table.item(row, scene_caption_col).text()
