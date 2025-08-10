from list import AbstractListWindow

class ShotlistWindow(AbstractListWindow):
    def __init__(self, ui):
        # Use the 'shotlists' folder for shotlists
        super().__init__(ui, subfolder="shotlists")
        self.setWindowTitle("Shotlist")

    # Optionally, you can alias signals for compatibility with old code
    @property
    def shotlist_status(self):
        return self.list_status

    @property
    def shot_caption_selected(self):
        return self.caption_selected

    @property
    def shot_timecodes(self):
        return self.timecodes_signal

    @property
    def is_last_available_shot(self):
        return self.is_last_available_row

    @property
    def is_first_available_shot(self):
        return self.is_first_available_row
