DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QLineEdit, QPushButton
)

from utility import minimum_load_interval, HIGHLIGHT_BACKGROUND_COLOR, HIGHLIGHT_COLOR

class GremlinsWindow(QMainWindow):
    """
    Chaos generator window that sends random events at specified intervals.
    Used for testing application robustness and simulating unpredictable user behavior.
    """
    
    # Signals
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    chaos = pyqtSignal()  # The chaos event signal
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.setWindowTitle("Gremlins")
        self.setMinimumSize(125, 70)

        # Add this for preferences compatibility
        self._pending_save_data = {}
        
        # State
        self.is_running = False
        self.interval_seconds = 3.0
        
        # Timer for chaos events
        self.chaos_timer = QTimer()
        self.chaos_timer.timeout.connect(self.emit_chaos)
        
        self.setup_ui(ui)
        
    def setup_ui(self, ui):
        """Set up the user interface"""
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 10, 0, 10)
        layout.setSpacing(2)
        
        # Spacer
        layout.addStretch()
        
        button_width, button_height = ui.get_dimensions("button")

        # Interval input field
        self.interval_field = QLineEdit()
        self.interval_field.setText(str(self.interval_seconds))
        self.interval_field.setPlaceholderText("seconds")
        self.interval_field.setFixedSize(button_width, button_height)
        self.interval_field.setAlignment(Qt.AlignCenter)
        self.interval_field.textChanged.connect(self.on_interval_changed)
        self.interval_field.setToolTip("Set chaos interval in seconds")
        layout.addWidget(self.interval_field, alignment=Qt.AlignCenter)
        
        # On/Off toggle button
        self.toggle_button = QPushButton("Off")
        self.toggle_button.setFixedSize(button_width, button_height)
        self.toggle_button.clicked.connect(self.toggle_chaos)
        layout.addWidget(self.toggle_button, alignment=Qt.AlignCenter)
        
        # Spacer
        layout.addStretch()
        
        central_widget.setLayout(layout)
        
    def on_interval_changed(self, text):
        """Handle interval field changes"""
        try:
            value = float(text)
            if value < minimum_load_interval:
                value = minimum_load_interval
                if DEBUG: print(f"DEBUG: Gremlins interval forced to {minimum_load_interval}s")
            
            # Update interval
            self.interval_seconds = value
            if self.is_running:
                # Restart timer with new interval
                self.chaos_timer.stop()
                self.chaos_timer.start(int(self.interval_seconds * 1000))
            if DEBUG: print(f"DEBUG: Gremlins interval changed to {self.interval_seconds}s")
            
        except ValueError:
            # Invalid float input (empty field, letters, etc.) - just ignore
            if DEBUG: print(f"DEBUG: Invalid Gremlins interval input: '{text}'")
            pass
            
    def toggle_chaos(self):
        """Toggle chaos generation on/off"""
        if self.is_running:
            self.stop_chaos()
        else:
            self.start_chaos()
            
    def start_chaos(self):
        """Start generating chaos events"""
        self.is_running = True
        self.toggle_button.setText("On")
        
        # Start the timer
        interval_ms = int(self.interval_seconds * 1000)
        self.chaos_timer.start(interval_ms)
        
        if DEBUG: print(f"DEBUG: Gremlins started - chaos every {self.interval_seconds}s")
        
    def stop_chaos(self):
        """Stop generating chaos events"""
        self.is_running = False
        self.toggle_button.setText("Off")
        
        # Stop the timer
        self.chaos_timer.stop()
        
        if DEBUG: print("DEBUG: Gremlins stopped")
        
    def emit_chaos(self):
        """Emit a chaos event"""
        if DEBUG: print("DEBUG: Gremlins emitting chaos event")
        
        # Blink the interval field
        self.blink_interval_field()
        
        # Emit the chaos signal
        self.chaos.emit()
        
    def blink_interval_field(self):
        """Make the interval field blink briefly"""
        # Store original style
        original_style = self.interval_field.styleSheet()
        
        # Set blink style (fuchsia background)
        self.interval_field.setStyleSheet(f"QLineEdit {{ color: {HIGHLIGHT_COLOR}; background-color: {HIGHLIGHT_BACKGROUND_COLOR}; }}")

        # Reset to original style after 90ms
        QTimer.singleShot(90, lambda: self.interval_field.setStyleSheet(original_style))
        
    def clear_project(self):
        """Clear project - stop chaos when switching projects"""
        if self.is_running:
            self.stop_chaos()
            
    def set_project_folder(self, project_folder):
        """Set project folder - no specific action needed"""
        pass
        
    def on_preferences_save(self):
        """Save preferences"""
        pass
        
    def on_preferences_load(self, prefs):
        """Load preferences"""
        pass
                            
    def closeEvent(self, event):
        """Handle window close"""
        if self.is_running:
            self.stop_chaos()
        super().closeEvent(event)