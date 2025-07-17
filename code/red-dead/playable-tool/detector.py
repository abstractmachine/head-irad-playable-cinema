from PyQt5.QtWidgets import QMainWindow, QWidget, QVBoxLayout, QTextEdit, QPushButton, QApplication
from PyQt5.QtCore import Qt

class DetectorWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Scene Detector")
        self.setGeometry(200, 200, 600, 200)

        # Central widget and layout
        central_widget = QWidget()
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(12)

        # Multi-line text field with scrollbars
        self.text_field = QTextEdit()
        self.text_field.setPlaceholderText("")
        self.text_field.setAlignment(Qt.AlignLeft)
        layout.addWidget(self.text_field)

        # Detect Scenes button
        self.detect_button = QPushButton("Detect Scenes")
        layout.addWidget(self.detect_button)

        self.setCentralWidget(central_widget)

        # where we will store the video path
        self.video_path = None

    def process_video(self, video_path):
        if video_path:
            self.video_path = video_path

    def handle_global_key(self, event):
        focus_widget = QApplication.focusWidget()
        from PyQt5.QtWidgets import QTextEdit
        if not isinstance(focus_widget, QTextEdit):
            self.keyPressEvent(event)