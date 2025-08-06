DEBUG = True  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal

class Switchboard(QObject):
    """
    Central coordinator that listens for various system events and manages state.
    
    The Switchboard acts as a hub for all inter-window communication, preventing
    direct dependencies between windows and ensuring clean separation of concerns.
    It manages project lifecycle events and coordinates responses across all windows.
    
    Key responsibilities:
    - Project loading/clearing coordination
    - Catalog loading status management
    - Metadata rebuild tracking
    - Cross-window state synchronization
    """
    
    # ---- SIGNALS ----
    # These signals are emitted by the switchboard to coordinate state across windows
    
    project_clearing = pyqtSignal()  # Emitted when switching projects to clear old state
    project_loaded = pyqtSignal(str)  # Emitted after project clear to set new project folder
    
    # Metadata rebuild coordination signals
    metadata_rebuilding_started = pyqtSignal()  # Emitted when first catalog starts rebuilding
    metadata_rebuilding_stopped = pyqtSignal()  # Emitted when last catalog finishes rebuilding
    
    def __init__(self, windows=None):
        """
        Initialize the switchboard with references to all application windows.
        
        Args:
            windows (dict): Dictionary mapping window names to window instances
                          e.g., {"project": ProjectWindow(), "cinematheque": CinemathequeWindow()}
        """
        super().__init__()
        self.current_shot_index = -1  # Track current shot for playback coordination
        self.current_project_folder = None  # Track current project to detect changes
        self.windows = windows  # Reference to all windows for signal routing
        self.catalogs_rebuilding = []  # Track which catalogs are currently rebuilding metadata
        
        if DEBUG: print("DEBUG: Switchboard initialized")
        
        # Set up all signal connections if windows are provided
        if self.windows:
            self.setup_connections()

    def setup_connections(self):
        """
        Set up all signal connections between windows through the switchboard.
        
        This is the central hub where all inter-window communication is established.
        All signals flow through the switchboard to maintain clean architecture.
        """
        if DEBUG: print("DEBUG: Switchboard setting up connections")
        
        # ---- PROJECT LIFECYCLE CONNECTIONS ----
        # These handle the core project loading/clearing workflow
        
        # Listen for project changes from the project window
        self.windows["project"].project_loaded.connect(self.project_folder_loaded)
        
        # When clearing projects, notify all windows to clean up their state
        self.project_clearing.connect(self.windows["shotlist"].clear_project)
        self.project_clearing.connect(self.windows["captions"].clear_project)
        self.project_clearing.connect(self.windows["subtitles"].clear_project)
        self.project_clearing.connect(self.windows["inference"].clear_project)
        self.project_clearing.connect(self.windows["cinematheque"].clear_project)
        self.project_clearing.connect(self.windows["playbill"].clear_project)
        
        # After clearing, set the new project folder in all windows
        self.project_loaded.connect(self.windows["shotlist"].set_project_folder)
        self.project_loaded.connect(self.windows["captions"].set_project_folder)
        self.project_loaded.connect(self.windows["subtitles"].set_project_folder)
        self.project_loaded.connect(self.windows["inference"].set_project_folder)
        self.project_loaded.connect(self.windows["prompt"].set_project_folder)
        self.project_loaded.connect(self.windows["cinematheque"].set_project_folder)
        self.project_loaded.connect(self.windows["playbill"].set_project_folder)

        # ---- METADATA REBUILD COORDINATION ----
        # These notify the project window about metadata rebuild status
        
        # Notify project window when metadata rebuilding starts/stops
        self.metadata_rebuilding_started.connect(self.windows["project"].on_metadata_rebuilding_started)
        self.metadata_rebuilding_stopped.connect(self.windows["project"].on_metadata_rebuilding_stopped)

        # ---- CATALOG STATUS CONNECTIONS ----
        # These monitor loading states of catalog windows (cinematheque, playbill)
        # and coordinate UI updates and cross-window dependencies
        
        # Cinematheque catalog status monitoring
        self.windows["cinematheque"].catalog_loading_started.connect(self.on_cinematheque_loading_started)
        self.windows["cinematheque"].catalog_loading_finished.connect(self.on_cinematheque_loading_finished)
        self.windows["cinematheque"].catalog_contents_cleared.connect(self.on_cinematheque_contents_cleared)
        
        # Playbill catalog status monitoring
        self.windows["playbill"].catalog_loading_started.connect(self.on_playbill_loading_started)
        self.windows["playbill"].catalog_loading_finished.connect(self.on_playbill_loading_finished)
        self.windows["playbill"].catalog_contents_cleared.connect(self.on_playbill_contents_cleared)

        # ---- METADATA REBUILD CONNECTIONS ----
        # These monitor metadata rebuild operations in catalog windows
        
        # Cinematheque metadata rebuild monitoring
        self.windows["cinematheque"].metadata_rebuild_started.connect(lambda: self.on_metadata_rebuild_started("cinematheque"))
        self.windows["cinematheque"].metadata_rebuild_finished.connect(lambda success: self.on_metadata_rebuild_finished("cinematheque", success))
        self.windows["cinematheque"].metadata_rebuild_cancelled.connect(lambda: self.on_metadata_rebuild_cancelled("cinematheque"))
        
        # Playbill metadata rebuild monitoring
        self.windows["playbill"].metadata_rebuild_started.connect(lambda: self.on_metadata_rebuild_started("playbill"))
        self.windows["playbill"].metadata_rebuild_finished.connect(lambda success: self.on_metadata_rebuild_finished("playbill", success))
        self.windows["playbill"].metadata_rebuild_cancelled.connect(lambda: self.on_metadata_rebuild_cancelled("playbill"))

        if DEBUG: print("DEBUG: Switchboard finished setting up connections")

    # ---- PROJECT LIFECYCLE HANDLERS ----

    def project_folder_loaded(self, project_folder):
        """
        Handle when a new project folder is loaded from the project window.
        
        This is the central coordination point for project changes. It ensures
        clean transitions by clearing old project state before loading new state.
        
        Args:
            project_folder (str): Path to the new project folder
        """
        if DEBUG: print(f"DEBUG: Switchboard: Project folder loaded: {project_folder}")
        
        # If switching between different projects (not just initial load)
        if self.current_project_folder and self.current_project_folder != project_folder:
            if DEBUG: print(f"DEBUG: Switchboard: Clearing previous project: {self.current_project_folder}")
            # First, tell all windows to clear their old project state
            # This stops any ongoing operations and resets UI
            self.project_clearing.emit()
        
        # Update our tracking and notify all windows of the new project
        self.current_project_folder = project_folder
        self.project_loaded.emit(project_folder)

    # ---- METADATA REBUILD HANDLERS ----

    def on_metadata_rebuild_started(self, catalog_name):
        """
        Handle when a catalog starts rebuilding metadata.
        
        Args:
            catalog_name (str): Name of the catalog ("cinematheque" or "playbill")
        """
        if DEBUG: print(f"DEBUG: Switchboard: Metadata rebuild started for {catalog_name}")
        
        # Check if this is the first catalog to start rebuilding
        was_empty = len(self.catalogs_rebuilding) == 0
        
        # Add to rebuilding list if not already present
        if catalog_name not in self.catalogs_rebuilding:
            self.catalogs_rebuilding.append(catalog_name)
            if DEBUG: print(f"DEBUG: Switchboard: Added {catalog_name} to rebuilding list. Current list: {self.catalogs_rebuilding}")
        
        # If this was the first catalog to start rebuilding, emit signal
        if was_empty and len(self.catalogs_rebuilding) > 0:
            if DEBUG: print("DEBUG: Switchboard: First catalog started rebuilding - emitting metadata_rebuilding_started")
            self.metadata_rebuilding_started.emit()

    def on_metadata_rebuild_finished(self, catalog_name, success):
        """
        Handle when a catalog finishes rebuilding metadata.
        
        Args:
            catalog_name (str): Name of the catalog ("cinematheque" or "playbill")
            success (bool): Whether the rebuild was successful
        """
        if DEBUG: print(f"DEBUG: Switchboard: Metadata rebuild finished for {catalog_name}, success: {success}")
        
        # Remove from rebuilding list
        if catalog_name in self.catalogs_rebuilding:
            self.catalogs_rebuilding.remove(catalog_name)
            if DEBUG: print(f"DEBUG: Switchboard: Removed {catalog_name} from rebuilding list. Current list: {self.catalogs_rebuilding}")
        
        # If this was the last catalog rebuilding, emit signal
        if len(self.catalogs_rebuilding) == 0:
            if DEBUG: print("DEBUG: Switchboard: Last catalog finished rebuilding - emitting metadata_rebuilding_stopped")
            self.metadata_rebuilding_stopped.emit()

    def on_metadata_rebuild_cancelled(self, catalog_name):
        """
        Handle when a catalog's metadata rebuild is cancelled.
        
        Args:
            catalog_name (str): Name of the catalog ("cinematheque" or "playbill")
        """
        if DEBUG: print(f"DEBUG: Switchboard: Metadata rebuild cancelled for {catalog_name}")
        
        # Remove from rebuilding list
        if catalog_name in self.catalogs_rebuilding:
            self.catalogs_rebuilding.remove(catalog_name)
            if DEBUG: print(f"DEBUG: Switchboard: Removed {catalog_name} from rebuilding list. Current list: {self.catalogs_rebuilding}")
        
        # If this was the last catalog rebuilding, emit signal
        if len(self.catalogs_rebuilding) == 0:
            if DEBUG: print("DEBUG: Switchboard: Last catalog cancelled rebuilding - emitting metadata_rebuilding_stopped")
            self.metadata_rebuilding_stopped.emit()

    def is_any_catalog_rebuilding(self):
        """
        Check if any catalog is currently rebuilding metadata.
        
        Returns:
            bool: True if any catalog is rebuilding, False otherwise
        """
        return len(self.catalogs_rebuilding) > 0

    def get_rebuilding_catalogs(self):
        """
        Get a copy of the list of catalogs currently rebuilding metadata.
        
        Returns:
            list: Copy of catalogs currently rebuilding
        """
        return self.catalogs_rebuilding.copy()

    # ---- CATALOG EVENT HANDLERS ----
    # These methods respond to loading/clearing events from catalog windows
    # and can coordinate cross-window responses

    def on_cinematheque_contents_cleared(self):
        """
        Handle when cinematheque catalog contents are cleared.
        
        This happens during:
        - Project switching
        - Metadata rebuilds
        - Manual clearing operations
        
        Can be used to disable UI elements in other windows that depend
        on cinematheque selections (e.g., movie-specific buttons).
        """
        if DEBUG: print("DEBUG: Switchboard: Cinematheque contents cleared")
        # Future: Add cross-window coordination logic here
        # Example: Disable movie-dependent buttons in other windows

    def on_playbill_contents_cleared(self):
        """
        Handle when playbill catalog contents are cleared.
        
        Similar to cinematheque clearing, but for playbill-specific content.
        Can coordinate UI updates in windows that depend on playbill selections.
        """
        if DEBUG: print("DEBUG: Switchboard: Playbill contents cleared")
        # Future: Add cross-window coordination logic here
        # Example: Clear related data in other windows

    def on_cinematheque_loading_started(self):
        """
        Handle when cinematheque starts loading catalog data.
        
        Delegates to the cinematheque window's own loading started handler
        to manage button states, progress display, etc. The switchboard
        can also coordinate related actions in other windows if needed.
        """
        if DEBUG: print("DEBUG: Switchboard: Cinematheque started loading")
        # Let cinematheque handle its own UI state changes
        self.windows["cinematheque"].on_catalog_loading_started()
        # Future: Add cross-window coordination if needed

    def on_cinematheque_loading_finished(self):
        """
        Handle when cinematheque finishes loading catalog data.
        
        Delegates to the cinematheque window's own loading finished handler
        to restore button states, hide progress, etc. Can also trigger
        dependent operations in other windows.
        """
        if DEBUG: print("DEBUG: Switchboard: Cinematheque finished loading")
        # Let cinematheque handle its own UI state changes
        self.windows["cinematheque"].on_catalog_loading_finished()
        # Future: Enable dependent features in other windows

    def on_playbill_loading_started(self):
        """
        Handle when playbill starts loading catalog data.
        
        Similar to cinematheque loading started, but for playbill content.
        Manages playbill-specific loading state and coordinates any
        cross-window dependencies.
        """
        if DEBUG: print("DEBUG: Switchboard: Playbill started loading")
        # Let playbill handle its own UI state changes
        self.windows["playbill"].on_catalog_loading_started()

    def on_playbill_loading_finished(self):
        """
        Handle when playbill finishes loading catalog data.
        
        Similar to cinematheque loading finished, but for playbill content.
        Restores playbill UI state and can trigger dependent operations.
        """
        if DEBUG: print("DEBUG: Switchboard: Playbill finished loading")
        # Let playbill handle its own UI state changes
        self.windows["playbill"].on_catalog_loading_finished()

    # ---- LEGACY/UNUSED HANDLERS ----
