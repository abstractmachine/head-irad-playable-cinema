DEBUG = True  # Or import this from your main module

# Qt
from PyQt5.QtCore import QObject, QEvent, Qt
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit, QPlainTextEdit

# Global key filter to handle key events across all windows
class GlobalKeyFilter(QObject):
    def __init__(self, windows, main_window):
        super().__init__()
        self.windows = windows  # This is now a dict
        self.main_window = main_window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyPress:
            # Avoid handling global shortcuts if focus is in a text field
            widget = QApplication.focusWidget()
            if isinstance(widget, (QLineEdit, QTextEdit, QPlainTextEdit)):
                return False  # Let the text field handle the key event

            cine = self.windows["cinematheque"]
            if event.key() == Qt.Key_PageDown:
                if DEBUG: print("DEBUG: PageDown pressed")
                cine.select_next_movie()
                return True
            elif event.key() == Qt.Key_PageUp:
                if DEBUG: print("DEBUG: PageUp pressed")
                cine.select_previous_movie()
                return True
            elif event.key() == Qt.Key_W:
                if DEBUG: print("DEBUG: W pressed (resetting dock layout)")
                from app import reset_dock_layout  # Import here to avoid circular imports
                reset_dock_layout(self.main_window)
                return True

            # Keep all other key handling for shortcuts (A, O, B, N, etc.)
            self.windows["nickelodeon"].handle_global_key(event)
            self.windows["captions"].keyPressEvent(event)
            return True
        return False