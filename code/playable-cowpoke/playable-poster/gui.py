"""
Minimal Qt GUI (PySide6). Run via app.py
Requires: pip install PySide6 pillow
"""
import sys
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton,
    QFileDialog, QHBoxLayout, QVBoxLayout, QMessageBox, QCheckBox
)
from PySide6.QtGui import QPixmap
from PySide6.QtCore import Qt
from PIL.ImageQt import ImageQt
import make
import images

WIDTH_CM = 68.6
HEIGHT_CM = 101.6
DPI_PREVIEW = 150
DPI_SAVE = 300
DEFAULT_FONT = "./fonts/P22FolkArt-Block.ttf"

class PosterWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Poster Creator (Qt)")
        self.resize(900, 700)

        # left: preview
        self.preview_label = QLabel(alignment=Qt.AlignCenter)
        self.preview_label.setMinimumSize(520, 680)

        # right: controls
        self.title_input = QLineEdit()
        self.title_input.setPlaceholderText("Title")
        self.title_input.textChanged.connect(self.update_preview)

        self.credits_input = QLineEdit()
        self.credits_input.setPlaceholderText("Credits (optional)")
        self.credits_input.textChanged.connect(self.update_preview)

        self.font_input = QLineEdit(DEFAULT_FONT)

        # new: use movie grid background
        self.use_grid_cb = QCheckBox("Fill with random movie frames (16)")
        self.use_grid_cb.stateChanged.connect(self.update_preview)

        self.create_btn = QPushButton("Create (save PDF)")
        self.create_btn.clicked.connect(self.create_file)

        right_layout = QVBoxLayout()
        right_layout.addWidget(QLabel("Title"))
        right_layout.addWidget(self.title_input)
        right_layout.addWidget(QLabel("Credits (optional)"))
        right_layout.addWidget(self.credits_input)
        right_layout.addWidget(QLabel("Font (optional)"))
        right_layout.addWidget(self.font_input)
        right_layout.addWidget(self.use_grid_cb)
        right_layout.addStretch()
        right_layout.addWidget(self.create_btn)

        layout = QHBoxLayout(self)
        layout.addWidget(self.preview_label, 1)
        layout.addLayout(right_layout)

        self.update_preview()

    def update_preview(self):
        title = self.title_input.text().strip() or None
        credits = self.credits_input.text().strip() or None
        font_path = self.font_input.text().strip() or None

        background = None
        if self.use_grid_cb.isChecked():
            try:
                background = images.grid_from_movies(num=16, cols=4,
                                                    width_cm=WIDTH_CM, height_cm=HEIGHT_CM,
                                                    dpi=DPI_PREVIEW, folder=images.MOVIES_DIR)
            except Exception:
                background = None

        pil = make.render_poster(width_cm=WIDTH_CM, height_cm=HEIGHT_CM, dpi=DPI_PREVIEW,
                                title=title, credits=credits, font_path=font_path,
                                background=background)
        qimg = ImageQt(pil)
        pix = QPixmap.fromImage(qimg)
        pix = pix.scaled(self.preview_label.width(), self.preview_label.height(),
                         Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.preview_label.setPixmap(pix)

    def create_file(self):
        title = self.title_input.text().strip() or None
        credits = self.credits_input.text().strip() or None
        font_path = self.font_input.text().strip() or None

        background = None
        if self.use_grid_cb.isChecked():
            try:
                background = images.grid_from_movies(num=16, cols=4,
                                                    width_cm=WIDTH_CM, height_cm=HEIGHT_CM,
                                                    dpi=DPI_SAVE, folder=images.MOVIES_DIR)
            except Exception:
                background = None

        path, _ = QFileDialog.getSaveFileName(self, "Save Poster", "poster.pdf", "PDF Files (*.pdf)")
        if not path:
            return
        try:
            make.make_poster(outfile=path, width_cm=WIDTH_CM, height_cm=HEIGHT_CM,
                             dpi=DPI_SAVE, title=title, credits=credits,
                             font_path=font_path, background=background)
            # success: saved silently
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Could not save poster:\n{e}")

# Add a run() helper so app.py can start the GUI
def run(argv=None):
    app = QApplication(argv if argv is not None else [])
    w = PosterWindow()
    w.show()
    return app.exec()