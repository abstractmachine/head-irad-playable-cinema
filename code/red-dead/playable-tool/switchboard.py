DEBUG = True  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal

class Switchboard(QObject):
    """
    Central coordinator that listens for various system events and manages state.
    This handles all the coordination logic for when movies are loaded, bots are toggled, etc.
    """
    
    # Project signals that the switchboard will emit to listeners
    project_loaded = pyqtSignal(str)  # Re-emit project_loaded to all listeners
    
    def __init__(self, windows=None):
        super().__init__()
        self.current_shot_index = -1
        self.current_project_folder = None
        self.windows = windows
        if DEBUG: print("DEBUG: Switchboard initialized")
        
        if self.windows:
            self.setup_connections()

    def setup_connections(self):
        """Setup all window-to-window connections through the switchboard"""
        if DEBUG: print("DEBUG: Switchboard setting up essential connections only")
        
        # === ESSENTIAL CONNECTIONS ONLY ===
        # Start with just the most critical connections to get basic functionality working
        
        # Project connections - these are fundamental
        self.windows["project"].project_loaded.connect(self.project_folder_loaded)
        self.project_loaded.connect(self.windows["shotlist"].set_project_folder)
        self.project_loaded.connect(self.windows["captions"].set_project_folder)
        self.project_loaded.connect(self.windows["subtitles"].set_project_folder)
        self.project_loaded.connect(self.windows["inference"].set_project_folder)
        self.project_loaded.connect(self.windows["prompt"].set_project_folder)
        self.project_loaded.connect(self.windows["cinematheque"].set_project_folder)
        self.project_loaded.connect(self.windows["playbill"].set_project_folder)

        if DEBUG: print("DEBUG: Switchboard finished setting up essential connections")

    def project_folder_loaded(self, project_folder):
        """Handle when a project folder is loaded and coordinate distribution to all listeners"""
        if DEBUG: print(f"DEBUG: Switchboard received: Project folder loaded: {project_folder}")
        self.current_project_folder = project_folder
        # Re-emit to all listeners through our centralized signal
        self.project_loaded.emit(project_folder)
