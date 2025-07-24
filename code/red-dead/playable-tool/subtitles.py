import os
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QComboBox, QSpinBox, QCheckBox, QSizePolicy
)

class SubtitlesWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Subtitles")
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # Title
        title_label = QLabel("Subtitle Export Settings")
        title_label.setStyleSheet("font-size: 16px; font-weight: bold; margin-bottom: 10px;")
        main_layout.addWidget(title_label)

        # Settings panel
        settings_layout = QHBoxLayout()
        
        # Export format
        format_layout = QVBoxLayout()
        format_layout.addWidget(QLabel("Export Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["SRT", "VTT", "ASS/SSA", "TXT"])
        format_layout.addWidget(self.format_combo)
        settings_layout.addLayout(format_layout)

        # Frame rate
        fps_layout = QVBoxLayout()
        fps_layout.addWidget(QLabel("Frame Rate:"))
        self.fps_spin = QSpinBox()
        self.fps_spin.setRange(1, 60)
        self.fps_spin.setValue(24)
        fps_layout.addWidget(self.fps_spin)
        settings_layout.addLayout(fps_layout)

        # Options
        options_layout = QVBoxLayout()
        options_layout.addWidget(QLabel("Options:"))
        self.include_timecodes = QCheckBox("Include Timecodes")
        self.include_timecodes.setChecked(True)
        self.ignore_empty = QCheckBox("Skip Empty Captions")
        self.ignore_empty.setChecked(True)
        options_layout.addWidget(self.include_timecodes)
        options_layout.addWidget(self.ignore_empty)
        settings_layout.addLayout(options_layout)

        settings_layout.addStretch()
        main_layout.addLayout(settings_layout)

        # Preview area
        preview_label = QLabel("Export Preview:")
        preview_label.setStyleSheet("font-weight: bold; margin-top: 20px;")
        main_layout.addWidget(preview_label)

        self.preview_field = QTextEdit()
        self.preview_field.setReadOnly(True)
        self.preview_field.setPlaceholderText("Subtitle preview will appear here...")
        
        # Load custom font
        font_path = os.path.join(os.path.dirname(__file__), "ui/fonts/HKGrotesk-Regular.otf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        font_families = QFontDatabase.applicationFontFamilies(font_id)
        if font_families:
            hk_font = QFont(font_families[0], 10)
        else:
            hk_font = QFont("Courier", 10)
        
        self.preview_field.setFont(hk_font)
        self.preview_field.setStyleSheet("QTextEdit { border: 1px solid #ccc; padding: 5px; background-color: #f8f8f8; }")
        main_layout.addWidget(self.preview_field, stretch=1)

        # Buttons
        button_layout = QHBoxLayout()
        button_layout.addStretch()
        
        self.generate_button = QPushButton("Generate Preview")
        self.generate_button.setFixedWidth(120)
        self.generate_button.clicked.connect(self.generate_preview)
        button_layout.addWidget(self.generate_button)
        
        self.export_button = QPushButton("Export File")
        self.export_button.setFixedWidth(120)
        self.export_button.clicked.connect(self.export_subtitles)
        button_layout.addWidget(self.export_button)
        
        button_layout.addStretch()
        main_layout.addLayout(button_layout)

        # Set up container
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Connect signals
        self.format_combo.currentTextChanged.connect(self.generate_preview)
        self.include_timecodes.toggled.connect(self.generate_preview)
        self.ignore_empty.toggled.connect(self.generate_preview)

    def generate_preview(self):
        """Generate subtitle preview based on current settings"""
        format_type = self.format_combo.currentText()
        include_tc = self.include_timecodes.isChecked()
        skip_empty = self.ignore_empty.isChecked()
        
        # Sample data for preview
        sample_data = [
            {"start": "00:00:05", "end": "00:00:08", "caption": "A man walks through the desert."},
            {"start": "00:00:10", "end": "00:00:15", "caption": ""},
            {"start": "00:00:18", "end": "00:00:22", "caption": "Close-up of weathered hands."},
        ]
        
        if format_type == "SRT":
            preview = self.generate_srt_preview(sample_data, skip_empty)
        elif format_type == "VTT":
            preview = self.generate_vtt_preview(sample_data, skip_empty)
        elif format_type == "TXT":
            preview = self.generate_txt_preview(sample_data, include_tc, skip_empty)
        else:
            preview = "ASS/SSA format preview not implemented yet."
        
        self.preview_field.setPlainText(preview)

    def generate_srt_preview(self, data, skip_empty):
        """Generate SRT format preview"""
        output = []
        counter = 1
        for item in data:
            if skip_empty and not item["caption"].strip():
                continue
            output.append(f"{counter}")
            output.append(f"{item['start']},000 --> {item['end']},000")
            output.append(item["caption"] or "[No caption]")
            output.append("")
            counter += 1
        return "\n".join(output)

    def generate_vtt_preview(self, data, skip_empty):
        """Generate VTT format preview"""
        output = ["WEBVTT", ""]
        for item in data:
            if skip_empty and not item["caption"].strip():
                continue
            output.append(f"{item['start']}.000 --> {item['end']}.000")
            output.append(item["caption"] or "[No caption]")
            output.append("")
        return "\n".join(output)

    def generate_txt_preview(self, data, include_tc, skip_empty):
        """Generate TXT format preview"""
        output = []
        for item in data:
            if skip_empty and not item["caption"].strip():
                continue
            if include_tc:
                output.append(f"[{item['start']} - {item['end']}] {item['caption'] or '[No caption]'}")
            else:
                output.append(item["caption"] or "[No caption]")
        return "\n".join(output)

    def export_subtitles(self):
        """Export subtitles to file"""
        # TODO: Implement actual export functionality
        # This would connect to your shotlist data
        print("Export subtitles - to be implemented")

    def on_request_save(self):
        """Save window preferences"""
        geo = self.geometry()
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height(),
            "format": self.format_combo.currentText(),
            "fps": self.fps_spin.value(),
            "include_timecodes": self.include_timecodes.isChecked(),
            "ignore_empty": self.ignore_empty.isChecked()
        }
        return self._pending_save_data

    def on_request_load(self, data):
        """Load window preferences"""
        if data:
            x = data.get("x", 100)
            y = data.get("y", 100)
            w = data.get("width", 700)
            h = data.get("height", 500)
            self.setGeometry(x, y, w, h)
            
            # Load settings
            if "format" in data:
                index = self.format_combo.findText(data["format"])
                if index >= 0:
                    self.format_combo.setCurrentIndex(index)
            
            if "fps" in data:
                self.fps_spin.setValue(data["fps"])
            
            if "include_timecodes" in data:
                self.include_timecodes.setChecked(data["include_timecodes"])
            
            if "ignore_empty" in data:
                self.ignore_empty.setChecked(data["ignore_empty"])
            
            # Update preview
            self.generate_preview()