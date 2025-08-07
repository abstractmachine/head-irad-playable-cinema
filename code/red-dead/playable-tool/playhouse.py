from player import AbstractPlayerWindow, SEEK_FAST

class PlayhouseWindow(AbstractPlayerWindow):
    def __init__(self, ui):
        super().__init__(ui)
        self.setWindowTitle("Playhouse")

    def seek_back(self):
        """Override to use SEEK_FAST for gameplay videos"""
        if not self.current_video_path:
            return
        seek_amount = SEEK_FAST  # Use fast seek instead of normal
        self.seek_video(-seek_amount)

    def seek_forward(self):
        """Override to use SEEK_FAST for gameplay videos"""
        if not self.current_video_path:
            return
        seek_amount = SEEK_FAST  # Use fast seek instead of normal
        self.seek_video(seek_amount)

    def handle_global_key(self, event):
        """Handle global key events"""
        # Placeholder for future key handling
        pass
