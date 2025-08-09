DEBUG = False  # Set to True to enable debug output

# Python imports
import os  # Add this import at the top
import random
# Qt imports
from PyQt5.QtCore import QObject, pyqtSignal

class Switchboard(QObject):
    """
    Central coordinator that listens for various system events and manages state.
    """
    
    # ---- SIGNALS ----
    # These signals are emitted by the switchboard to coordinate state across windows
    
    project_clearing = pyqtSignal()  # Emitted when switching projects to clear old state
    project_loaded = pyqtSignal(str)  # Emitted after project clear to set new project folder
    
    # Chaos event signal
    chaos_event = pyqtSignal()  # Emitted when chaos event occurs (from gremlins robots)

    # Metadata rebuild coordination signals
    metadata_rebuilding_started = pyqtSignal()  # Emitted when first catalog starts rebuilding
    metadata_rebuilding_stopped = pyqtSignal()  # Emitted when last catalog finishes rebuilding
    
    def __init__(self, windows=None):
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
        if DEBUG: print("DEBUG: Switchboard setting up connections")
        
        # ---- PROJECT LIFECYCLE CONNECTIONS ----
        
        # Listen for project changes from the robots window
        self.windows["robots"].project_loaded.connect(self.project_folder_loaded)
        
        # When clearing projects, notify all windows to clean up their state
        self.project_clearing.connect(self.windows["captions"].clear_project)
        self.project_clearing.connect(self.windows["inference"].clear_project)
        self.project_clearing.connect(self.windows["robots"].clear_project)
        
        # Add player clearing connections
        self.project_clearing.connect(self.windows["nickelodeon"].clear_project)
        self.project_clearing.connect(self.windows["playhouse"].clear_project)
        
        # After clearing, set the new project folder in all windows
        self.project_loaded.connect(self.windows["captions"].project_folder_was_set)
        self.project_loaded.connect(self.windows["inference"].project_folder_was_set)
        self.project_loaded.connect(self.windows["prompt"].project_folder_was_set)

        # ---- METADATA REBUILD COORDINATION ----
        
        # Notify robots window when metadata rebuilding starts/stops
        self.metadata_rebuilding_started.connect(self.windows["robots"].on_metadata_rebuilding_started)
        self.metadata_rebuilding_stopped.connect(self.windows["robots"].on_metadata_rebuilding_stopped)

        # ---- CATALOG STATUS CONNECTIONS ----

        self.project_clearing.connect(self.windows["cinematheque"].clear_project)
        self.project_clearing.connect(self.windows["playbill"].clear_project)
        self.windows["cinematheque"].catalog_loading_started.connect(self.cinematheque_loading_started)
        self.windows["cinematheque"].catalog_loading_finished.connect(self.cinematheque_loading_finished)
        self.windows["cinematheque"].catalog_contents_cleared.connect(self.cinematheque_contents_cleared)
        
        self.project_loaded.connect(self.windows["cinematheque"].project_folder_was_set)
        self.project_loaded.connect(self.windows["playbill"].project_folder_was_set)
        self.windows["playbill"].catalog_loading_started.connect(self.playbill_loading_started)
        self.windows["playbill"].catalog_loading_finished.connect(self.playbill_loading_finished)
        self.windows["playbill"].catalog_contents_cleared.connect(self.playbill_contents_cleared)

        # ---- CATALOG ITEM SELECTION CONNECTIONS ----
        # These monitor item selections in catalog windows and coordinate responses
        
        # Cinematheque item selection monitoring
        self.windows["cinematheque"].item_might_change.connect(self.cinematheque_item_might_change)
        self.windows["cinematheque"].item_selected.connect(self.cinematheque_item_selected)
        
        # Playbill item selection monitoring
        self.windows["playbill"].item_might_change.connect(self.playbill_item_might_change)
        self.windows["playbill"].item_selected.connect(self.playbill_item_selected)

        # ---- METADATA REBUILD CONNECTIONS ----
        
        # Cinematheque metadata rebuild monitoring
        self.windows["cinematheque"].metadata_rebuild_started.connect(lambda: self.metadata_rebuild_started("cinematheque"))
        self.windows["cinematheque"].metadata_rebuild_finished.connect(lambda success: self.metadata_rebuild_finished("cinematheque", success))
        self.windows["cinematheque"].metadata_rebuild_cancelled.connect(lambda: self.metadata_rebuild_cancelled("cinematheque"))
        
        # Playbill metadata rebuild monitoring
        self.windows["playbill"].metadata_rebuild_started.connect(lambda: self.metadata_rebuild_started("playbill"))
        self.windows["playbill"].metadata_rebuild_finished.connect(lambda success: self.metadata_rebuild_finished("playbill", success))
        self.windows["playbill"].metadata_rebuild_cancelled.connect(lambda: self.metadata_rebuild_cancelled("playbill"))

        # ---- SUBTITLE CONNECTIONS ----
        self.project_clearing.connect(self.windows["subtitles"].clear_project)
        self.project_loaded.connect(self.windows["subtitles"].project_folder_was_set)
        self.windows["nickelodeon"].video_did_load.connect(self.windows["subtitles"].on_movie_loaded)
        self.windows["nickelodeon"].timecode_changed.connect(self.windows["subtitles"].on_timecode_changed)

        # ---- SHOTLIST CONNECTIONS ----
        self.project_clearing.connect(self.windows["shotlist"].clear_project)
        self.project_loaded.connect(self.windows["shotlist"].project_folder_was_set)
        self.windows["nickelodeon"].video_did_load.connect(self.windows["shotlist"].on_movie_loaded)
        self.windows["nickelodeon"].timecode_changed.connect(self.windows["shotlist"].on_timecode_changed)
        self.windows["shotlist"].jump_to_timecode_signal.connect(self.windows["nickelodeon"].jump_to_timecode)

        # ---- CAPTION CONNECTIONS ----
        self.windows["shotlist"].shot_caption_selected.connect(self.windows["captions"].set_shot_caption_field)

        # ---- CHAOS EVENT CONNECTIONS ----
        self.windows["robots"].chaos.connect(self.on_chaos_event)

        if DEBUG: print("DEBUG: Switchboard finished setting up connections")

    # ---- PROJECT LIFECYCLE HANDLERS ----

    def project_folder_loaded(self, project_folder):
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

    def metadata_rebuild_started(self, catalog_name):
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

    def metadata_rebuild_finished(self, catalog_name, success):
        if DEBUG: print(f"DEBUG: Switchboard: Metadata rebuild finished for {catalog_name}, success: {success}")
        
        # Remove from rebuilding list
        if catalog_name in self.catalogs_rebuilding:
            self.catalogs_rebuilding.remove(catalog_name)
            if DEBUG: print(f"DEBUG: Switchboard: Removed {catalog_name} from rebuilding list. Current list: {self.catalogs_rebuilding}")
        
        # If this was the last catalog rebuilding, emit signal
        if len(self.catalogs_rebuilding) == 0:
            if DEBUG: print("DEBUG: Switchboard: Last catalog finished rebuilding - emitting metadata_rebuilding_stopped")
            self.metadata_rebuilding_stopped.emit()

    def metadata_rebuild_cancelled(self, catalog_name):
        if DEBUG: print(f"DEBUG: Switchboard: Metadata rebuild cancelled for {catalog_name}")
        
        # Remove from rebuilding list
        if catalog_name in self.catalogs_rebuilding:
            self.catalogs_rebuilding.remove(catalog_name)
            if DEBUG: print(f"DEBUG: Switchboard: Removed {catalog_name} from rebuilding list. Current list: {self.catalogs_rebuilding}")
        
        # If this was the last catalog rebuilding, emit signal
        if len(self.catalogs_rebuilding) == 0:
            if DEBUG: print("DEBUG: Switchboard: Last catalog cancelled rebuilding - emitting metadata_rebuilding_stopped")
            self.metadata_rebuilding_stopped.emit()

    # ---- CATALOG EVENT HANDLERS ----

    def cinematheque_contents_cleared(self):
        if DEBUG: print("DEBUG: Switchboard: Cinematheque contents cleared")
        
        # Disable cinematheque-specific buttons when contents are cleared
        self.windows["cinematheque"].disable_shotlist_bot_button()

    def playbill_contents_cleared(self):
        if DEBUG: print("DEBUG: Switchboard: Playbill contents cleared")

    def cinematheque_loading_started(self):
        if DEBUG: print("DEBUG: Switchboard: Cinematheque started loading")

        # Let cinematheque handle its own UI state changes
        self.windows["cinematheque"].on_catalog_loading_started()

    def cinematheque_loading_finished(self):
        if DEBUG: print("DEBUG: Switchboard: Cinematheque finished loading")

        # Let cinematheque handle its own UI state changes
        self.windows["cinematheque"].on_catalog_loading_finished()

    def playbill_loading_started(self):
        if DEBUG: print("DEBUG: Switchboard: Playbill started loading")

        # Let playbill handle its own UI state changes
        self.windows["playbill"].on_catalog_loading_started()

    def playbill_loading_finished(self):
        if DEBUG: print("DEBUG: Switchboard: Playbill finished loading")

        # Let playbill handle its own UI state changes
        self.windows["playbill"].on_catalog_loading_finished()

    # ---- CATALOG ITEM SELECTION HANDLERS ----

    def cinematheque_item_might_change(self, metadata):
        if DEBUG: print(f"DEBUG: Switchboard: Cinematheque item might change: {metadata['title']}")

        self.windows["cinematheque"].disable_bot_buttons()

    def cinematheque_item_selected(self, metadata, timecode=None):
        if DEBUG:
            if timecode is None:
                print(f"DEBUG: Switchboard: Cinematheque item selected: {metadata['title']} with no timecode")
            elif isinstance(timecode, str):
                print(f"DEBUG: Switchboard: Cinematheque item selected: {metadata['title']} with timecode string {timecode}")
            elif isinstance(timecode, int):
                print(f"DEBUG: Switchboard: Cinematheque item selected: {metadata['title']} with timecode int {timecode}")
            elif isinstance(timecode, float):
                print(f"DEBUG: Switchboard: Cinematheque item selected: {metadata['title']} with timecode float {timecode}")
        
        # Load video in Nickelodeon player
        filename = metadata.get('filename', '')
        if filename and self.current_project_folder:
            movie_path = os.path.join(self.current_project_folder, "movies", filename)
            if os.path.exists(movie_path):
                if DEBUG: print(f"DEBUG: Loading movie in Nickelodeon: {movie_path}")
                self.windows["nickelodeon"].load_video(movie_path, metadata, timecode)
            else:
                if DEBUG: print(f"DEBUG: Movie file not found: {movie_path}")
        
        # Enable buttons when an item is selected
        self.windows["cinematheque"].enable_bot_buttons()

    def playbill_item_might_change(self, metadata):
        if DEBUG: print(f"DEBUG: Switchboard: Playbill item might change: {metadata['title']}")

        self.windows["playbill"].disable_bot_buttons()

    def playbill_item_selected(self, metadata, timecode=None):
        if DEBUG:
            if timecode is None:
                print(f"DEBUG: Switchboard: Playbill item selected: {metadata['title']} with no timecode")
            elif isinstance(timecode, str):
                print(f"DEBUG: Switchboard: Playbill item selected: {metadata['title']} with timecode string {timecode}")
            elif isinstance(timecode, int):
                print(f"DEBUG: Switchboard: Playbill item selected: {metadata['title']} with timecode int {timecode}")
            elif isinstance(timecode, float):
                print(f"DEBUG: Switchboard: Playbill item selected: {metadata['title']} with timecode float {timecode}")
        
        # Load video in Playhouse player
        filename = metadata.get('filename', '')
        if filename and self.current_project_folder:
            gameplay_path = os.path.join(self.current_project_folder, "gameplay", filename)
            if os.path.exists(gameplay_path):
                if DEBUG: print(f"DEBUG: Loading gameplay in Playhouse: {gameplay_path}")
                self.windows["playhouse"].load_video(gameplay_path, metadata, timecode)
            else:
                if DEBUG: print(f"DEBUG: Gameplay file not found: {gameplay_path}")
        
        # Enable buttons when an item is selected
        self.windows["playbill"].enable_bot_buttons()

    # --------- GREMLINS --------------

    # Add this method at the end of the class:
    def on_chaos_event(self):
        """Handle chaos events from the gremlins robots"""
        # choice = random.choice(["cinematheque", "playbill"])
        choice = "cinematheque" # force to Cinematheque for testing
        # Choose randomly from Playbill or Cinematheque
        if choice == "cinematheque":
            if DEBUG: print("DEBUG: Chaos event - triggering Cinematheque")
            self.windows["cinematheque"].emit_chaos_event()
        else:
            if DEBUG: print("DEBUG: Chaos event - triggering Playbill")
            self.windows["playbill"].emit_chaos_event()

