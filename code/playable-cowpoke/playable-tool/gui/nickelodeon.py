from PyQt5.QtCore import Qt
from gui.player import AbstractPlayerWindow, SEEK_NORMAL, SEEK_FAST

class NickelodeonWindow(AbstractPlayerWindow):
    def __init__(self, ui):
        super().__init__(ui)
        self.setWindowTitle("Nickelodeon")
        self.setObjectName("Nickelodeon")

    def handle_global_key(self, event):
        """Handle global key events"""
        key = event.key()
        modifiers = event.modifiers()
        
        if key == Qt.Key_Space:
            self.toggle_play_pause()
        elif key == Qt.Key_Left:
            if not self.current_video_path:  # Check before seeking
                return
            if modifiers & Qt.ShiftModifier:
                self.seek_video(-SEEK_FAST)  # Use SEEK_FAST constant
            else:
                self.seek_video(-SEEK_NORMAL)  # Use SEEK_NORMAL constant
        elif key == Qt.Key_Right:
            if not self.current_video_path:  # Check before seeking
                return
            if modifiers & Qt.ShiftModifier:
                self.seek_video(SEEK_FAST)  # Use SEEK_FAST constant
            else:
                self.seek_video(SEEK_NORMAL)  # Use SEEK_NORMAL constant
