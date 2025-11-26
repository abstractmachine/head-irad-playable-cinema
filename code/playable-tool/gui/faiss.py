DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal

class FaissModule(QObject):
    """
    FAISS module that processes timecode changes when activated.
    This is not a window - just a background processing module.
    """
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)
    
    # Signal to send messages to robots console
    console_message = pyqtSignal(str)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.is_active = False
        self.project_folder = None
        self.last_shot_caption = ""  # Track previous shot caption
    
    def toggle(self):
        """Toggle FAISS mode on/off"""
        if self.is_active:
            self.deactivate()
        else:
            self.activate()
    
    def activate(self):
        """Activate FAISS mode"""
        self.is_active = True
        self.last_shot_caption = ""  # Reset when activating
        self.console_message.emit("FAISS on")
        if DEBUG: print("DEBUG: FAISS mode activated")
    
    def deactivate(self):
        """Deactivate FAISS mode"""
        self.is_active = False
        self.last_shot_caption = ""  # Reset when deactivating
        self.console_message.emit("FAISS off")
        if DEBUG: print("DEBUG: FAISS mode deactivated")
    
    def on_timecode_changed(self, timecode_ms):
        """Called when playhouse video timecode changes"""
        if not self.is_active:
            return
        
        # Convert milliseconds to HH:MM:SS format
        seconds = timecode_ms // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        
        timecode_str = f"{h:02}:{m:02}:{s:02}"
        
        # Send processing message to console
        self.console_message.emit(f"[FAISS] Processing frame at {timecode_str}")
        if DEBUG: print(f"DEBUG: FAISS processing at {timecode_str}")
    
    def on_shot_caption_changed(self, caption):
        """Called when the current shot caption changes in playlist"""
        if not self.is_active:
            return
        
        # Only process if caption is non-empty and different from last
        if caption and caption != self.last_shot_caption:
            self.last_shot_caption = caption
            self.console_message.emit(f"[FAISS] Shot: {caption}")
            if DEBUG: print(f"DEBUG: FAISS shot caption changed to: {caption}")
    
    def on_project_folder_loaded(self, project_folder):
        """Set the project folder"""
        self.project_folder = project_folder
        if DEBUG: print(f"DEBUG: FAISS project folder set to {project_folder}")
    
    def clear_project(self):
        """Clear project"""
        self.project_folder = None
        self.last_shot_caption = ""
        if self.is_active:
            self.deactivate()
    
    def on_preferences_save(self):
        """Save preferences"""
        self._pending_save_data = {
            "is_active": self.is_active
        }
        return self._pending_save_data
    
    def on_preferences_load(self, data):
        """Load preferences"""
        if data and data.get("is_active"):
            self.activate()