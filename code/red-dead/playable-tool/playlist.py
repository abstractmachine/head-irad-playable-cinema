from list import AbstractListWindow

class PlaylistWindow(AbstractListWindow):
    def __init__(self, ui):
        # Use the 'playlists' folder for playlists
        super().__init__(ui, subfolder="playlists")
        self.setWindowTitle("Playlist")
