from player import AbstractPlayerWindow

class PlayhouseWindow(AbstractPlayerWindow):
    def __init__(self, ui):
        super().__init__(ui)
        self.setWindowTitle("Playhouse")

    def handle_global_key(self, event):
        """Handle global key events"""
        #key = event.key()
        #modifiers = event.modifiers()
        