from PyQt5.QtWidgets import QMainWindow, QVBoxLayout, QWidget, QLineEdit, QPushButton, QLabel
from PyQt5.QtCore import Qt
from PyQt5.QtCore import pyqtSignal

class AnnotateWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, player_window, detector_window):
        super().__init__()
        self.player_window = player_window
        self.detector_window = detector_window

        self.setWindowTitle("Annotate")
        self.setGeometry(400, 200, 400, 150)

        layout = QVBoxLayout()
        self.caption_field = QLineEdit()
        self.caption_field.setPlaceholderText("Enter caption here...")
        self.caption_field.setEnabled(False)
        self.caption_field.returnPressed.connect(self.activate_caption_field)
        layout.addWidget(QLabel("Caption:"))
        layout.addWidget(self.caption_field)

        self.annotate_button = QPushButton("Annotate")
        self.annotate_button.setEnabled(False)
        self.annotate_button.clicked.connect(self.annotate_caption)
        layout.addWidget(self.annotate_button)

        container = QWidget()
        container.setLayout(layout)
        self.setCentralWidget(container)

        # Enable caption field on click
        self.caption_field.mousePressEvent = self.activate_caption_field_event

        # Listen for shotlist changes
        self.detector_window.scene_table.itemSelectionChanged.connect(self.update_annotate_state)
        self.detector_window.scene_table.model().rowsInserted.connect(self.update_annotate_state)
        self.detector_window.scene_table.model().rowsRemoved.connect(self.update_annotate_state)

    def activate_caption_field(self):
        self.caption_field.setEnabled(True)
        self.caption_field.setFocus()

    def activate_caption_field_event(self, event):
        self.activate_caption_field()
        QLineEdit.mousePressEvent(self.caption_field, event)

    def update_annotate_state(self):
        # Enable annotate if shotlist is loaded
        enabled = self.detector_window.scene_table.rowCount() > 0
        self.annotate_button.setEnabled(enabled)
        self.caption_field.setEnabled(enabled)

    def annotate_caption(self):
        # Get current video timecode
        tc = self.player_window.timecode
        # Find the corresponding row in the shotlist
        table = self.detector_window.scene_table
        for row in range(table.rowCount()):
            start_tc = table.item(row, 0).text()
            end_tc = table.item(row, 1).text()
            if self.timecode_in_range(tc, start_tc, end_tc):
                table.setItem(row, 2, QLabel(self.caption_field.text()))
                break

    def timecode_in_range(self, tc, start_tc, end_tc):
        def tc_to_ms(tc):
            parts = tc.split(":")
            if len(parts) == 3:
                h = int(parts[0])
                m = int(parts[1])
                s = float(parts[2])
                return int((h * 3600 + m * 60 + s) * 1000)
            return 0
        ms = tc_to_ms(tc)
        start_ms = tc_to_ms(start_tc)
        end_ms = tc_to_ms(end_tc)
        return start_ms <= ms <= end_ms
    
    def on_request_save(self):
        # Save window geometry (position and size)
        geo = self.geometry()
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height()
        }
        return self._pending_save_data

    def on_request_load(self, data):
        # Restore window geometry if available
        if data:
            x = data.get("x", 400)
            y = data.get("y", 200)
            w = data.get("width", 400)
            h = data.get("height", 150)
            self.setGeometry(x, y, w, h)