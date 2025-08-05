DEBUG = True  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal

class Selector(QObject):
    """
    Central coordinator that listens for various system events and manages state.
    This handles all the coordination logic for when movies are loaded, bots are toggled, etc.
    """
    
    def __init__(self):
        super().__init__()
        self.current_shot_index = -1
        if DEBUG: print("DEBUG: Selector initialized")

    def cinematheque_started_loading(self):
        if DEBUG: print("DEBUG: Selector received: Cinematheque started loading its database")

    def started_loading_new_movie_with_metadata(self):
        if DEBUG: print("DEBUG: Selector received: Started loading a new movie")
        # Reset shot index when starting to load a new movie
        self.current_shot_index = -1
        if DEBUG: print(f"DEBUG: Selector received: Shot index reset to {self.current_shot_index}")
    
    def new_movie_loaded_with_metadata(self, video_path, metadata):
        if DEBUG: print(f"DEBUG: Selector received: New Movie Loaded with all its Metadata: {video_path}")

    def shot_index_changed(self, shot_index):
        if DEBUG: print(f"DEBUG: Selector received: Shot index changed from {self.current_shot_index} to {shot_index}")
        self.current_shot_index = shot_index
