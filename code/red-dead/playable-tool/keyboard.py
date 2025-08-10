DEBUG = False  # Or import this from your main module

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

            cinematheque = self.windows["cinematheque"]
            if event.key() == Qt.Key_PageDown:
                if DEBUG: print("DEBUG: PageDown pressed")
                cinematheque.select_next_item()
                return True
            elif event.key() == Qt.Key_PageUp:
                if DEBUG: print("DEBUG: PageUp pressed")
                cinematheque.select_previous_item()
                return True

            # Keep all other key handling for shortcuts (A, O, B, N, etc.)
            self.windows["nickelodeon"].handle_global_key(event)
            self.windows["shotlist"].handle_global_key(event)
            return True
        return False