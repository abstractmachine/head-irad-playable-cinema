from gui.list import AbstractListWindow

class ShotlistWindow(AbstractListWindow):
    def __init__(self, ui):
        # Use the 'shotlists' folder for shotlists
        super().__init__(ui, subfolder="shotlists")
        self.setWindowTitle("Shotlist")
