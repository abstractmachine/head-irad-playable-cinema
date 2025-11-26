DEBUG = True  # Set to True to enable debug output

# Python imports
import os
import random
import pandas as pd
from PyQt5.QtCore import QTimer
# Qt imports
from PyQt5.QtCore import QObject, pyqtSignal

class Switchboard(QObject):
    """
    Central coordinator that listens for various system events and manages state.
    """
    
    # ---- SIGNALS ----
    # These signals are emitted by the switchboard to coordinate state across windows
    
    project_cleared = pyqtSignal()  # Emitted when switching projects to clear old state
    project_loaded = pyqtSignal(str)  # Emitted after project clear to set new project folder
    
    # Chaos event signal
    chaos_event = pyqtSignal()  # Emitted when chaos event occurs (from gremlins robots)

    # Metadata rebuild coordination signals
    metadata_rebuilding_started = pyqtSignal()  # Emitted when first catalog starts rebuilding
    metadata_rebuilding_stopped = pyqtSignal()  # Emitted when last catalog finishes rebuilding

    def __init__(self, windows, ui, keyboard):
        super().__init__()
        self.windows = windows
        self.ui = ui
        self.keyboard = keyboard
        
        # Get references to commonly used windows
        self.robots = windows.get("robots")
        self.faiss = windows.get("faiss")
        self.playhouse = windows.get("playhouse")
        
        # Connect all signals
        self.connect_signals()

        self.current_shot_index = -1  # Track current shot for playback coordination
        self.current_project_folder = None  # Track current project to detect changes
        self.catalogs_rebuilding = []  # Track which catalogs are currently rebuilding metadata
        
        if DEBUG: print("DEBUG: Switchboard initialized")
        
        # Set up all signal connections if windows are provided
        if self.windows:
            self.setup_connections()

    def connect_signals(self):
        """Connect all inter-window signals"""
        
        # FAISS connections
        if self.robots and self.faiss:
            # Toggle FAISS on/off when robots button is clicked
            self.robots.faiss_toggle_requested.connect(self.faiss.toggle)
            
            # Send FAISS console messages to robots console
            self.faiss.console_message.connect(self.robots.on_faiss_message)
            
            # Handle FAISS match jumps
            self.faiss.jump_to_movie_shot.connect(self.on_faiss_match)
            
            if DEBUG: print("DEBUG: Switchboard: FAISS signals connected")
        
        # Playhouse timecode to FAISS
        if self.playhouse and self.faiss:
            # Send timecode changes to FAISS for processing
            self.playhouse.timecode_changed.connect(self.faiss.on_timecode_changed)
            
            if DEBUG: print("DEBUG: Switchboard: Playhouse -> FAISS timecode connected")
        
        # Caption changes to FAISS (connect to caption window directly)
        captions = self.windows.get("captions")
        if captions and self.faiss:
            # Listen to play shot caption edits
            captions.caption_was_edited.connect(self.on_caption_edited_for_faiss)
            
            if DEBUG: print("DEBUG: Switchboard: Captions -> FAISS caption connected")

    def setup_connections(self):
        if DEBUG: print("DEBUG: Switchboard setting up connections")
        
        # ---- PROJECT LIFECYCLE CONNECTIONS ----
        
        # Listen for project changes from the robots window
        self.windows["robots"].project_folder_was_set.connect(self.project_folder_loaded)

        # When clearing projects, notify all windows to clean up their state
        self.project_cleared.connect(self.windows["captions"].clear_project)
        self.project_cleared.connect(self.windows["inference"].clear_project)
        self.project_cleared.connect(self.windows["robots"].clear_project)

        # Add player clearing connections
        self.project_cleared.connect(self.windows["nickelodeon"].clear_project)
        self.project_cleared.connect(self.windows["playhouse"].clear_project)

        # After clearing, set the new project folder in all windows
        self.project_loaded.connect(self.windows["cinematheque"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["playbill"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["shotlist"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["playlist"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["captions"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["inference"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["prompt"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["subtitles"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["robots"].on_project_folder_loaded)
        self.project_loaded.connect(self.windows["faiss"].on_project_folder_loaded)

        # --------- TEXT SIZE CHANGES ------------

        self.keyboard.increase_text_size.connect(self.ui.increase_text_size)
        self.keyboard.decrease_text_size.connect(self.ui.decrease_text_size)
        self.ui.text_size_changed.connect(self.windows["captions"].set_text_size)

        # ---- METADATA REBUILD COORDINATION ----
        
        # Notify robots window when metadata rebuilding starts/stops
        self.metadata_rebuilding_started.connect(self.windows["robots"].on_metadata_rebuilding_started)
        self.metadata_rebuilding_stopped.connect(self.windows["robots"].on_metadata_rebuilding_stopped)

        # ---- CATALOG STATUS CONNECTIONS ----

        self.project_cleared.connect(self.windows["cinematheque"].clear_project)
        self.project_cleared.connect(self.windows["playbill"].clear_project)

        self.windows["cinematheque"].catalog_loading_started.connect(self.cinematheque_loading_started)
        self.windows["cinematheque"].catalog_loading_finished.connect(self.cinematheque_loading_finished)
        self.windows["cinematheque"].catalog_contents_cleared.connect(self.cinematheque_contents_cleared)
        
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

        # ---- SUBTITLE CONNECTIONS (ONLY CONCERNS MOVIES) ----
        self.project_cleared.connect(self.windows["subtitles"].clear_project)
        self.windows["nickelodeon"].video_did_load.connect(self.windows["subtitles"].on_movie_loaded)
        self.windows["nickelodeon"].timecode_changed.connect(self.windows["subtitles"].on_timecode_changed)

        # ---- LIST CONNECTIONS ----
        self.project_cleared.connect(self.windows["shotlist"].clear_project)
        self.project_cleared.connect(self.windows["playlist"].clear_project)

        self.windows["nickelodeon"].video_is_loading.connect(self.nickelodeon_is_loading)
        self.windows["playhouse"].video_is_loading.connect(self.playhouse_is_loading)
        self.windows["nickelodeon"].video_did_load.connect(self.windows["shotlist"].on_movie_loaded)
        self.windows["playhouse"].video_did_load.connect(self.windows["playlist"].on_movie_loaded)
        self.windows["nickelodeon"].timecode_changed.connect(self.windows["shotlist"].on_timecode_changed)
        self.windows["playhouse"].timecode_changed.connect(self.windows["playlist"].on_timecode_changed)
        
        self.windows["shotlist"].jump_to_timecode_signal.connect(self.windows["nickelodeon"].jump_to_timecode)
        self.windows["playlist"].jump_to_timecode_signal.connect(self.windows["playhouse"].jump_to_timecode)

        # ---- CAPTION CONNECTIONS ----
        self.windows["shotlist"].shot_caption_selected.connect(
            lambda caption: self.windows["captions"].set_caption("movie", "shot", caption)
        )
        self.windows["shotlist"].scene_caption_selected.connect(
            lambda caption: self.windows["captions"].set_caption("movie", "scene", caption)
        )
        self.windows["playlist"].shot_caption_selected.connect(
            lambda caption: self.windows["captions"].set_caption("play", "shot", caption)
        )
        self.windows["playlist"].scene_caption_selected.connect(
            lambda caption: self.windows["captions"].set_caption("play", "scene", caption)
        )

        # ----------- SCENE BREAK -------------
        self.windows["robots"].break_scene_of_type.connect(self.on_break_scene_of_type)
        self.keyboard.delete_button_pressed.connect(self.on_delete_button_pressed)

        # --------- API CONNECTION ------------
        self.windows["robots"].api_start_call.connect(self.on_api_start_call)

        # ---- CHAOS EVENT CONNECTIONS ----
        self.windows["robots"].chaos.connect(self.on_chaos_event)

        # --- Inference Buttons from Robots ---
        self.windows["robots"].caption_model_requested.connect(self.windows["inference"].select_caption_model)
        self.windows["robots"].search_model_requested.connect(self.windows["inference"].select_search_model)
        self.windows["robots"].inference_off_requested.connect(self.windows["inference"].turn_off_inference)

        # Caption changes from Captions window
        self.windows["captions"].caption_was_edited.connect(self.on_caption_edited)

        # Notify robots when shotlist/playlist status changes
        self.windows["shotlist"].list_status.connect(
            lambda loaded: self.windows["robots"].on_shotlist_status_changed(loaded)
        )
        self.windows["playlist"].list_status.connect(
            lambda loaded: self.windows["robots"].on_playlist_status_changed(loaded)
        )

        # Notify robots when movie is loaded in either player
        self.windows["nickelodeon"].video_did_load.connect(
            lambda *args, **kwargs: self.windows["robots"].on_video_loaded("movie")
        )
        self.windows["playhouse"].video_did_load.connect(
            lambda *args, **kwargs: self.windows["robots"].on_video_loaded("play")
        )

        # Notify robots when movie is loading
        self.windows["nickelodeon"].video_is_loading.connect(
            lambda: self.windows["robots"].on_video_loading("movie")
        )
        self.windows["playhouse"].video_is_loading.connect(
            lambda: self.windows["robots"].on_video_loading("play")
        )
        
        # Connect playhouse video cleared/stopped to disable FAISS
        if hasattr(self.windows["playhouse"], "video_cleared"):
            self.windows["playhouse"].video_cleared.connect(
                lambda: self.windows["robots"].on_video_cleared("play")
            )

        if DEBUG: print("DEBUG: Switchboard finished setting up connections")

    # ---- PROJECT LIFECYCLE HANDLERS ----

    def project_load_folder(self, project_folder):
        if DEBUG: print(f"DEBUG: Switchboard: Project folder loaded: {project_folder}")
        
        # If switching between different projects (not just initial load)
        if self.current_project_folder and self.current_project_folder != project_folder:
            if DEBUG: print(f"DEBUG: Switchboard: Clearing previous project: {self.current_project_folder}")
            # First, tell all windows to clear their old project state
            # This stops any ongoing operations and resets UI
            self.project_clearing.emit()

    def project_folder_loaded(self, project_folder):
        if DEBUG: print(f"DEBUG: Switchboard: Project folder loaded: {project_folder}")
        
        # First update our tracking
        self.current_project_folder = project_folder

        # Then notify all windows of the new project folder
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

    # --------- SCENE BREAK ----------

    def on_break_scene_of_type(self, play_or_movie):
        if DEBUG: print(f"DEBUG: Switchboard: Breaking scene for {play_or_movie}")
        # based on type
        if play_or_movie == "play":
            self.windows["playlist"].on_break_scene()
        elif play_or_movie == "movie":
            self.windows["shotlist"].on_break_scene()
        else:
            if DEBUG: print(f"DEBUG: Switchboard: Unknown type for breaking scene: {play_or_movie}")

    def on_delete_button_pressed(self):
        if DEBUG: print("DEBUG: Switchboard: Delete button pressed")
        # check to see if there is a row that needs to be deleted in the two lists
        self.windows["shotlist"].delete_selected()
        self.windows["playlist"].delete_selected()

    # ---- CATALOG EVENT HANDLERS ----

    def cinematheque_contents_cleared(self):
        if DEBUG: print("DEBUG: Switchboard: Cinematheque contents cleared")

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

    # ----------- PLAYER HANDLERS -------------

    def nickelodeon_is_loading(self):
        if DEBUG: print("DEBUG: Switchboard: Nickelodeon started loading")

        # Let shotlist handle its own UI state changes
        self.windows["shotlist"].on_movie_loading()

    def playhouse_is_loading(self):
        if DEBUG: print("DEBUG: Switchboard: Playhouse started loading")

        # Let playlist handle its own UI state changes
        self.windows["playlist"].on_movie_loading()

    # ---- CATALOG ITEM SELECTION HANDLERS ----

    def cinematheque_item_might_change(self, metadata):
        if DEBUG: print(f"DEBUG: Switchboard: Cinematheque item might change: {metadata['title']}")

        self.windows["cinematheque"].disable_bot_buttons()

    def playbill_item_might_change(self, metadata):
        if DEBUG: print(f"DEBUG: Switchboard: Playbill item might change: {metadata['title']}")

        self.windows["playbill"].disable_bot_buttons()

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


    # ----------- API --------------------

    def on_api_start_call(self, api_type, api_ilk):
        if DEBUG: print(f"DEBUG: API start call - type: {api_type}, ilk: {api_ilk}")
        print(f"DEBUG: API start call - type: {api_type}, ilk: {api_ilk}")

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

    def on_caption_edited(self, source, caption_type, text):
        # Handle caption edits here
        if DEBUG: print(f"DEBUG: Caption edited: {source} {caption_type}: {text}")
        # Route this to other windows or save as needed
        if source == "movie":
            self.windows["shotlist"].caption_was_edited(caption_type, text)
        elif source == "play":
            self.windows["playlist"].caption_was_edited(caption_type, text)
    
    def on_caption_edited_for_faiss(self, source, caption_type, text):
        """Route play shot caption changes to FAISS"""
        if DEBUG: print(f"DEBUG: Caption edited for FAISS: {source} {caption_type}: {text}")
        
        # Only send play shot captions to FAISS
        if source == "play" and caption_type == "shot":
            if DEBUG: print(f"DEBUG: Sending to FAISS: {text}")
            self.faiss.on_shot_caption_changed(text)
    
    def on_faiss_match(self, video_name, shot_index):
        """Handle FAISS match - jump cinematheque to the matched movie and shot"""
        if DEBUG: print(f"DEBUG: FAISS match - video: {video_name}, shot: {shot_index}")

        cine = self.windows.get("cinematheque")
        if not cine:
            if DEBUG: print("DEBUG: No cinematheque window available")
            return

        # Defer until list has items
        if cine.item_list.count() == 0:
            if DEBUG: print("DEBUG: Cinematheque not ready, deferring FAISS jump by 250ms")
            QTimer.singleShot(250, lambda: self.on_faiss_match(video_name, shot_index))
            return

        # --- timecode from shotlist ---
        import os, pandas as pd
        if not getattr(self, "current_project_folder", None):
            if DEBUG: print("DEBUG: No current project folder set")
            return

        shotlist_path = os.path.join(self.current_project_folder, "shotlists", f"{video_name}.csv")
        if not os.path.exists(shotlist_path):
            if DEBUG: print(f"DEBUG: Shotlist not found: {shotlist_path}")
            return

        try:
            df_shots = pd.read_csv(shotlist_path)
            if shot_index >= len(df_shots):
                if DEBUG: print(f"DEBUG: Shot index {shot_index} out of range for {video_name}")
                return
            start_timecode = df_shots.iloc[shot_index].get("Start", None)
            if DEBUG: print(f"DEBUG: Found timecode {start_timecode} for shot {shot_index} in {video_name}")
        except Exception as e:
            if DEBUG: print(f"DEBUG: Error reading shotlist CSV: {e}")
            return

        # --- resolve filename/title from cinematheque.csv ---
        meta_path = os.path.join(self.current_project_folder, "metadata", "cinematheque.csv")
        try:
            df_meta = pd.read_csv(
                meta_path,
                engine="python",
                quotechar='"',
                escapechar='\\',
                sep=',',
                on_bad_lines='skip'
            )
        except Exception as e:
            if DEBUG: print(f"DEBUG: Error reading cinematheque.csv: {e}")
            return

        required_cols = {"filename", "title"}
        if not required_cols.issubset(df_meta.columns):
            if DEBUG: print(f"DEBUG: cinematheque.csv missing columns: {required_cols - set(df_meta.columns)}")
            return

        target_filename = f"{video_name}.mp4"
        target_stem = os.path.splitext(target_filename)[0]

        def norm(s):
            return str(s or "").strip().lower()

        row = df_meta.loc[df_meta["filename"].apply(norm) == norm(target_filename)]
        if row.empty:
            row = df_meta.loc[df_meta["filename"].apply(lambda x: norm(os.path.splitext(str(x))[0])) == norm(target_stem)]
        if row.empty:
            if DEBUG: print(f"DEBUG: Could not resolve metadata for {target_filename} in cinematheque.csv")
            return

        resolved_title = str(row.iloc[0]["title"]).strip()
        resolved_filename = str(row.iloc[0]["filename"]).strip()
        if DEBUG: print(f"DEBUG: Resolved metadata: title='{resolved_title}', filename='{resolved_filename}'")

        # --- DEBUG dump + robust match using widget.item_data ---
        if DEBUG:
            print("DEBUG: Cinematheque items (first 20):")
            for i in range(min(20, cine.item_list.count())):
                item = cine.item_list.item(i)
                widget = cine.item_list.itemWidget(item)
                if not widget or not hasattr(widget, "item_data"):
                    continue
                md = widget.item_data or {}
                md_fn = md.get("filename", "")
                md_title = md.get("title", "")
                print(f"  [{i}] title='{md_title}' filename='{md_fn}'")

        found_widget = None
        found_data = None
        for i in range(cine.item_list.count()):
            item = cine.item_list.item(i)
            widget = cine.item_list.itemWidget(item)
            if not widget or not hasattr(widget, "item_data"):
                continue
            md = widget.item_data or {}
            fn = md.get("filename", "")
            stem = os.path.splitext(fn)[0]
            title = md.get("title", "")

            if (norm(fn) == norm(resolved_filename) or
                norm(stem) == norm(target_stem) or
                norm(title) == norm(resolved_title)):
                found_widget = widget
                found_data = md
                if DEBUG:
                    print(f"DEBUG: Matched Cinematheque item at row {i} (title='{title}', filename='{fn}')")
                break

        if not found_data:
            if DEBUG:
                print(f"DEBUG: Cinematheque item not found for filename='{resolved_filename}' "
                      f"(stem='{target_stem}', title='{resolved_title}')")
            return

        # Use Cinematheque click handler so it emits item_selected and updates selection
        try:
            cine.on_widget_clicked(found_widget, found_data, start_timecode)
            if DEBUG: print(f"DEBUG: Triggered Cinematheque selection for '{resolved_title}' at {start_timecode}")
        except Exception as e:
            if DEBUG: print(f"DEBUG: Error triggering Cinematheque selection: {e}")
            self.cinematheque_item_selected(found_data, start_timecode)
            if DEBUG: print(f"DEBUG: Routed selection via switchboard for '{resolved_title}' at {start_timecode}")
