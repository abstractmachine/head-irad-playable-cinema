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
        if DEBUG: print("DEBUG: Switchboard setting up all window connections")
        
        # Project connections
        self.windows["project"].project_loaded.connect(self.project_folder_loaded)
        self.project_loaded.connect(self.windows["shotlist"].set_project_folder)
        self.project_loaded.connect(self.windows["captions"].set_project_folder)
        self.project_loaded.connect(self.windows["subtitles"].set_project_folder)
        self.project_loaded.connect(self.windows["inference"].set_project_folder)
        self.project_loaded.connect(self.windows["prompt"].set_project_folder)
        self.project_loaded.connect(self.windows["cinematheque"].set_project_folder)
        self.project_loaded.connect(self.windows["playbill"].set_project_folder)

        # Movie loading connections
        self.windows["nickelodeon"].video_loaded_with_metadata.connect(self.windows["shotlist"].on_movie_loaded_with_metadata)
        self.windows["nickelodeon"].video_loaded_with_metadata.connect(self.windows["cinematheque"].on_movie_loaded_with_metadata)
        self.windows["nickelodeon"].video_loaded_with_metadata.connect(self.windows["prompt"].on_movie_loaded_with_metadata)
        self.windows["nickelodeon"].video_loaded_with_metadata.connect(self.windows["captions"].on_movie_loaded_with_metadata)
        self.windows["nickelodeon"].video_loaded_with_metadata.connect(self.windows["subtitles"].on_movie_loaded_with_metadata)
        self.windows["nickelodeon"].video_loaded_with_metadata.connect(self.windows["inference"].on_movie_loaded_with_metadata)
        
        # Gameplay loading connections
        self.windows["playhouse"].video_loaded_with_metadata.connect(self.windows["playbill"].on_gameplay_loaded_with_metadata)

        # Video timecode connections
        self.windows["nickelodeon"].video_timecode_changed.connect(self.windows["shotlist"].clear_table_selection)
        self.windows["nickelodeon"].video_timecode_changed.connect(self.windows["shotlist"].set_current_time)
        self.windows["nickelodeon"].video_timecode_changed.connect(self.windows["subtitles"].on_timecode_changed)
        self.windows["nickelodeon"].video_timecode_changed.connect(self.windows["inference"].on_timecode_changed)
        self.windows["nickelodeon"].frames_extracted.connect(self.windows["captions"].handle_api_frames)

        # Bot and automation connections
        self.windows["cinematheque"].request_caption_bot_autostart.connect(self.windows["captions"].enable_auto_start_bot)
        self.windows["cinematheque"].shotlist_bot_start.connect(self.windows["shotlist"].start_shotlist_bot)
        
        # Selection connections
        self.windows["cinematheque"].movie_selected.connect(self.windows["nickelodeon"].load_video_from_path_with_metadata)
        self.windows["playbill"].gameplay_selected.connect(self.windows["playhouse"].load_video_from_path_with_metadata)

        # Shotlist connections
        self.windows["shotlist"].jump_to_timecode_signal.connect(self.windows["nickelodeon"].jump_to_timecode)
        self.windows["shotlist"].shotlist_status.connect(self.windows["captions"].set_shotlist_status)
        self.windows["shotlist"].shot_caption_selected.connect(self.windows["captions"].set_shot_caption_field)
        self.windows["shotlist"].abort_api.connect(self.windows["captions"].handle_api_abort)
        self.windows["shotlist"].shot_timecodes.connect(self.windows["nickelodeon"].handle_shot_timecodes)
        self.windows["shotlist"].is_last_available_shot.connect(self.windows["captions"].handle_is_last_available_shot)
        self.windows["shotlist"].is_first_available_shot.connect(self.windows["captions"].handle_is_first_available_shot)
        self.windows["shotlist"].shotlist_status.connect(self.windows["cinematheque"].on_shotlist_status)
        self.windows["shotlist"].row_data.connect(self.windows["captions"].handle_row_data)
        self.windows["shotlist"].row_data.connect(self.windows["prompt"].handle_row_data)
        self.windows["shotlist"].row_did_change.connect(self.windows["prompt"].handle_row_did_change)

        # Caption connections
        self.windows["captions"].shot_caption_submitted.connect(self.windows["shotlist"].update_shot_caption_for_current_shot)
        self.windows["captions"].request_current_shot.connect(self.windows["shotlist"].handle_request_current_shot)
        self.windows["captions"].request_next_shot.connect(self.windows["shotlist"].jump_to_next_shot)
        self.windows["captions"].request_previous_shot.connect(self.windows["shotlist"].jump_to_previous_shot)
        self.windows["captions"].shot_bot_finished.connect(self.windows["cinematheque"].shot_bot_finished)

    def project_folder_loaded(self, project_folder):
        """Handle when a project folder is loaded and coordinate distribution to all listeners"""
        if DEBUG: print(f"DEBUG: Switchboard received: Project folder loaded: {project_folder}")
        self.current_project_folder = project_folder
        # Re-emit to all listeners through our centralized signal
        self.project_loaded.emit(project_folder)
