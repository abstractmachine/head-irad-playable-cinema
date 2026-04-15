DEBUG = False  # Or import this from your main module

# Qt
from PyQt5.QtCore import QObject, QEvent, Qt, pyqtSignal
from PyQt5.QtWidgets import QApplication, QLineEdit, QTextEdit, QPlainTextEdit

# Global key filter to handle key events across all windows
class GlobalKeyFilter(QObject):

    # signal we want to change text size
    increase_text_size = pyqtSignal()
    decrease_text_size = pyqtSignal()
    delete_button_pressed = pyqtSignal()

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

            if event.key() == Qt.Key_PageDown:
                if DEBUG: print("DEBUG: PageDown pressed")
                self.windows["cinematheque"].select_next_item()
                return True
            elif event.key() == Qt.Key_PageUp:
                if DEBUG: print("DEBUG: PageUp pressed")
                self.windows["cinematheque"].select_previous_item()
                return True
            elif event.key() == Qt.Key_Tab:
                if DEBUG: print("DEBUG: Tab pressed")
                self.windows["robots"].break_scene()
                return True
            elif event.key() == Qt.Key_Delete or event.key() == Qt.Key_Backspace:
                if DEBUG: print("DEBUG: Delete pressed")
                self.delete_button_pressed.emit()
                return True
            elif event.key() == Qt.Key_Plus or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_Equal):
                if DEBUG: print("DEBUG: Increase text size")
                self.increase_text_size.emit()  # Emit signal to increase text size
                return True
            elif event.key() == Qt.Key_Minus or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_Underscore):
                if DEBUG: print("DEBUG: Decrease text size")
                self.decrease_text_size.emit()  # Emit signal to decrease text size
                return True

            # Keep all other key handling for shortcuts (A, O, B, N, etc.)
            self.windows["nickelodeon"].handle_global_key(event)
            self.windows["shotlist"].handle_global_key(event)
            return True
        return False