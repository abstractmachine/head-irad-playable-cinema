from PyQt5.QtCore import Qt
from player import AbstractPlayerWindow

class NickelodeonWindow(AbstractPlayerWindow):
    def __init__(self, ui):
        super().__init__(ui)
        self.setWindowTitle("Nickelodeon")

    def handle_global_key(self, event):
        """Handle global key events"""
        key = event.key()
        modifiers = event.modifiers()
        
        if key == Qt.Key_Space:
            if self.current_video_path and self.vlc_player:  # Only if video loaded
                self.toggle_play_pause()
        elif key == Qt.Key_L or key == Qt.Key_V:
            self.load_video()
        elif key == Qt.Key_Left:
            if not self.current_video_path or not self.vlc_player:  # Check before seeking
                return
            if modifiers & Qt.ShiftModifier:
                seek_amount = float(self.fast_seek.text())
                self.seek_video(-seek_amount)
            else:
                seek_amount = float(self.normal_seek.text())
                self.seek_video(-seek_amount)
        elif key == Qt.Key_Right:
            if not self.current_video_path or not self.vlc_player:  # Check before seeking
                return
            if modifiers & Qt.ShiftModifier:
                seek_amount = float(self.fast_seek.text())
                self.seek_video(seek_amount)
            else:
                seek_amount = float(self.normal_seek.text())
                self.seek_video(seek_amount)
