DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QSize, QThread, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QSizePolicy, 
    QFileDialog, QMessageBox
)
from PyQt5.QtGui import QPixmap, QColor
import os
import csv
from metadata import MetadataWorker  # Import our metadata worker

# Common font size for all text
POSTER_WIDTH = 40
POSTER_HEIGHT = 60
ITEM_HEIGHT = 63
HIGHLIGHT_COLOR = "#f0f"
DARK_ITEM_BACKGROUND = "#444"
LIGHT_ITEM_BACKGROUND = "#ddd"
DARK_TEXT_COLOR = "#fff"
LIGHT_TEXT_COLOR = "#000"

class CinemathequeWindow(QMainWindow):
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    movie_selected = pyqtSignal(str, dict)  # Signal to send movie file path AND metadata to player
    project_loaded = pyqtSignal(str)  # Signal when project folder is loaded
    shotlist_bot_start = pyqtSignal()  # Signal to start shotlist bot
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        self.project_folder = None
        self.currently_loading_video = None  # Track what video is currently being requested
        self.selected_movie_widget = None  # Track currently selected movie widget
         
        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        layout = QVBoxLayout()
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)  # Set margins to 0
        
        # Movie list viewer
        self.movie_list = QListWidget()
        self.movie_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.movie_list.setAlternatingRowColors(False)
        self.movie_list.setSpacing(0)

        # DISABLE default Qt selection behavior
        self.movie_list.setSelectionMode(QListWidget.NoSelection)
        
        # Connect to our custom click handler only
        self.movie_list.itemClicked.connect(self.on_movie_clicked)
        layout.addWidget(self.movie_list)
        
        # Button layout
        button_layout = QHBoxLayout()
        button_layout.setContentsMargins(0, 0, 0, 0)  # Set margins to 0
        button_layout.setSpacing(0)  # Set spacing to 0
        button_width, button_height = self.ui.get_dimensions('button')

        # Project folder button
        self.project_folder_button = QPushButton("Project Folder")
        self.project_folder_button.setFont(self.ui.get_font('button'))
        self.project_folder_button.clicked.connect(self.select_project_folder)
        self.project_folder_button.setFixedSize(button_width, button_height)

        # Metadata rebuild button
        self.metadata_button = QPushButton("Rebuild Metadata")
        self.metadata_button.setFont(self.ui.get_font('button'))
        self.metadata_button.clicked.connect(self.rebuild_metadata)
        self.metadata_button.setEnabled(False)
        self.metadata_button.setFixedSize(160, button_height)

        # Bot buttons
        self.shotlist_bot_button = QPushButton("Shotlist Bot Off")
        self.shotlist_bot_button.setFont(self.ui.get_font('button'))
        self.shotlist_bot_button.setFixedSize(140, button_height)
        self.shotlist_bot_button.clicked.connect(self.handle_shotlist_bot)
        self.disable_shotlist_bot_button()

        self.scene_bot_button = QPushButton("Scene Bot Off")
        self.scene_bot_button.setFont(self.ui.get_font('button'))
        self.scene_bot_button.setFixedSize(140, button_height)
        self.scene_bot_button.setEnabled(False)
        self.scene_bot_button.clicked.connect(self.handle_caption_bot)

        self.caption_bot_button = QPushButton("Caption Bot Off")
        self.caption_bot_button.setFont(self.ui.get_font('button'))
        self.caption_bot_button.setFixedSize(140, button_height)
        self.caption_bot_button.setEnabled(False)
        self.caption_bot_button.clicked.connect(self.handle_caption_bot)

        button_layout.addWidget(self.project_folder_button)
        button_layout.addWidget(self.metadata_button)
        button_layout.addWidget(self.shotlist_bot_button)
        button_layout.addWidget(self.scene_bot_button)
        button_layout.addWidget(self.caption_bot_button)
        button_layout.addStretch()

        # Connect signals
        self.shotlist_bot_active = False
        self.shotlist_bot_anim_timer = QTimer()
        self.shotlist_bot_anim_timer.timeout.connect(self.animate_shotlist_bot)
        self.shotlist_bot_dots = 0

        layout.addLayout(button_layout)
        main_widget.setLayout(layout)
        
        # Initialize thread variables
        self.metadata_thread = None
        self.metadata_worker = None
        
        # Initialize animation timer for rebuilding button
        self.rebuild_animation_timer = QTimer()
        self.rebuild_animation_timer.timeout.connect(self.animate_rebuild_button)
        self.rebuild_dot_count = 0
        
        # Connect preference signals
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)
    
    def select_project_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if not folder:  # User cancelled
            return
        
        self.set_project_folder(folder)
    
    def set_project_folder(self, folder):
        """Set the project folder and check for required folders"""

        if DEBUG: print(f"DEBUG: Cinematheque: Setting project folder to {folder}")
        
        # Don't reload if it's the same folder
        if self.project_folder == folder:
            if DEBUG: print("DEBUG: Cinematheque: Project folder already set to this folder, skipping reload")
            # EMIT THE SIGNAL EVEN IF IT'S THE SAME FOLDER
            self.project_loaded.emit(folder)
            return
        
        self.check_missing_folders_and_files(folder)
        
        # Set the project folder and load project
        self.project_folder = folder
        self.project_folder_button.setText(f"Project")
        self.project_loaded.emit(folder)
        self.load_project(folder)

    def check_missing_folders_and_files(self, folder):
        """Check if all required folders and files exist in the project folder"""
        
        if DEBUG: print(f"DEBUG: Cinematheque: Checking missing folders and files in {folder}")        
        # Required project folders
        required_folders = ["datasets", "gameplay", "metadata", "movies", "posters", "preferences", "shotlists", "subtitles"]
        # Some folders need to add a .gitignore
        add_gitignore_folders = ["movies", "gameplay", "posters", "preferences", "subtitles"]
        # Some folders need these text files in them
        required_text_files = ["preferences/openai_api_key.txt", "preferences/tmdb_api_key.txt", "preferences/opensubtitles_api_key.txt"]
        
        # Check if all required folders exist
        missing_folders = []
        for required_folder in required_folders:
            folder_path = os.path.join(folder, required_folder)
            if not os.path.exists(folder_path):
                missing_folders.append(required_folder)
        
        # If any folders are missing, prompt user to create these folders for them
        if missing_folders:
            # Show warning dialog
            message = "The following project folder(s) could not be found:\n" + "\n".join(missing_folders) + \
                      "\n\nDo you want to create now?"
            reply = QMessageBox.question(self, "Missing Folders", message, 
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                return
            # Create missing folders
            try:
                for folder_name in missing_folders:
                    folder_path = os.path.join(folder, folder_name)
                    os.makedirs(folder_path, exist_ok=True)
                    # Check to see if we need to add a .gitignore
                    if folder_name in add_gitignore_folders:
                        gitignore_path = os.path.join(folder_path, ".gitignore")
                        if not os.path.exists(gitignore_path):
                            try:
                                with open(gitignore_path, 'w') as f:
                                    f.write("# Ignore all files in this folder (Remove this to reactivate git syncing)\n*\n")
                            except Exception as e:
                                QMessageBox.critical(self, "Error", f"Failed to create .gitignore:\n{str(e)}")
                                return
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to create folders:\n{str(e)}")
                return
            
        # Check if required text files exist
        for required_file in required_text_files:
            file_path = os.path.join(folder, required_file)
            # if this file does not exist, just create it
            if not os.path.exists(file_path):
                try:
                    # Create the parent directories if they don't exist
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, 'w') as f:
                        f.write("")  # Create an empty file
                except Exception as e:
                    QMessageBox.critical(self, "Error", f"Failed to create {required_file}:\n{str(e)}")
                    return
        
        # If we reach here, all folders and files are present or created successfully
        if DEBUG: print("DEBUG: All required folders and files are present.")
    
    def load_project(self, folder_path):
        """Called when a project folder is selected or loaded from preferences"""
        # Enable buttons when project is loaded
        self.metadata_button.setEnabled(True)
        
        # Check if metadata.csv exists
        metadata_path = os.path.join(folder_path, "metadata", "metadata.csv")
        
        if os.path.exists(metadata_path):
            self.load_movies_from_metadata(metadata_path, folder_path)
        else:
            # No metadata file, show placeholder
            self.movie_list.clear()
            
            # Add placeholder item
            placeholder_item = QListWidgetItem("No metadata.csv found. Click 'Rebuild Metadata' to create it.")
            self.movie_list.addItem(placeholder_item)

    def load_movies_from_metadata(self, metadata_path, project_folder):
        """Load movies from metadata.csv file"""
        self.movie_list.clear()
        self.selected_movie_widget = None  # Clear selection when reloading
        posters_folder = os.path.join(project_folder, "posters")
        
        try:
            with open(metadata_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                movie_count = 0
                for row in reader:
                    # Create custom widget for this movie, passing UI instance
                    movie_widget = MovieItemWidget(row, posters_folder, self.ui)
                    
                    # Create list item with fixed height
                    item = QListWidgetItem()
                    item.setSizeHint(QSize(movie_widget.width(), ITEM_HEIGHT))

                    # Add to list
                    self.movie_list.addItem(item)
                    self.movie_list.setItemWidget(item, movie_widget)
                    movie_count += 1

            # now that we've loaded, update the list
            self.update_movie_list()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load metadata.csv:\n{str(e)}")
            self.disable_shotlist_bot_button()

    def update_movie_list(self):
        # have each item update its background color
        for i in range(self.movie_list.count()):
            item = self.movie_list.item(i)
            if item:
                movie_widget = self.movie_list.itemWidget(item)
                if movie_widget:
                    movie_widget.update_background()
    
    def shot_bot_finished(self):
        """Handle bot finished signal"""
        
        if DEBUG: print("DEBUG: Cinematheque: bot finished")
        # Vertify if we are at the end of the list
        if self.movie_list.count() == 0:
            if DEBUG: print("DEBUG: Cinematheque: No movies to process")
            return
        
        # Get the last item in the list
        last_item = self.movie_list.item(self.movie_list.count() - 1)
        # Compare our selected movie widget with the last item
        last_widget = self.movie_list.itemWidget(last_item)
        if self.selected_movie_widget and self.selected_movie_widget == last_widget:
            if DEBUG: print("DEBUG: Cinematheque: Bot reached last movie, stopping")
            return
        
        # If we reach here, it means the bot is still active
        if DEBUG: print("DEBUG: Cinematheque: More movies remaining, select next movie")

        # Work todo here to handle the selecting the new movie
        # Then emitting a message to tell the bot to start processing the next movie

    def turn_off_all_bots(self):
        """Turn off all running bots and reset their buttons."""
        if self.shotlist_bot_active:
            self.shotlist_bot_active = False
            self.shotlist_bot_button.setText("Shotlist Bot Off")
            self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: center; }")
            self.shotlist_bot_anim_timer.stop()
        self.scene_bot_button.setText("Scene Bot Off")
        self.scene_bot_button.setStyleSheet("QPushButton { text-align: center; }")
        self.caption_bot_button.setText("Caption Bot Off")
        self.caption_bot_button.setStyleSheet("QPushButton { text-align: center; }")

    def on_movie_clicked(self, item):
        """Handle movie item click with custom selection"""
        # make sure to turn off all bots when a movie is clicked
        self.turn_off_all_bots()
        
        # Get the MovieItemWidget from the clicked item
        movie_widget = self.movie_list.itemWidget(item)
        if movie_widget and hasattr(movie_widget, 'movie_data'):
            
            # Clear previous selection FIRST (remove fuchsia background)
            if self.selected_movie_widget and self.selected_movie_widget != movie_widget:
                self.selected_movie_widget.set_selected(False)
                # Force immediate update
                self.selected_movie_widget.update()
        
            # Clear any Qt selection
            self.movie_list.clearSelection()
            
            # Small delay to ensure previous selection is cleared before setting new one
            QTimer.singleShot(10, lambda: self._set_new_selection(movie_widget))

    def _set_new_selection(self, movie_widget):
        """Helper method to set new selection after a small delay"""
        # Prevent selection if a video is already loading
        if self.currently_loading_video is not None:
            return
        movie_widget.set_selected(True)
        movie_widget.update()  # Force immediate update
        self.selected_movie_widget = movie_widget

        movie_data = movie_widget.movie_data
        filename = movie_data.get('filename', '')

        if filename and self.project_folder:
            movie_path = os.path.join(self.project_folder, "movies", filename)
            if os.path.exists(movie_path):
                self.currently_loading_video = movie_path
                self.movie_selected.emit(movie_path, movie_data)
            else:
                QMessageBox.warning(self, "File Not Found", f"Movie file not found:\n{movie_path}")

        QTimer.singleShot(20, self.update_shotlist_bot_button_state)

    def update_shotlist_bot_button_state(self):
        self.enable_shotlist_bot_button()

    def rebuild_metadata(self):
        """Start metadata rebuild process in worker thread"""
        if not self.project_folder:
            QMessageBox.warning(self, "Warning", "Please select a project folder first.")
            return
        
        # Check if API key files exist in preferences folder
        tmdb_key_path = os.path.join(os.path.dirname(__file__), 'preferences/tmdb_api_key.txt')
        opensubtitles_key_path = os.path.join(os.path.dirname(__file__), 'preferences/opensubtitles_api_key.txt')
        
        if not os.path.exists(tmdb_key_path):
            QMessageBox.critical(self, "Error", "preferences/tmdb_api_key.txt file not found.")
            return
            
        if not os.path.exists(opensubtitles_key_path):
            QMessageBox.critical(self, "Error", "preferences/opensubtitles_api_key.txt file not found.")
            return
        
        # Disable buttons during rebuild
        self.metadata_button.setText("        Rebuilding")
        self.metadata_button.setEnabled(False)
        self.project_folder_button.setEnabled(False)
        
        # Set button text alignment to left during rebuild
        self.metadata_button.setStyleSheet("QPushButton { text-align: left; }")
        
        # Start animated dots
        self.rebuild_dot_count = 0
        self.rebuild_animation_timer.start(500)  # Update every 500ms
        
        # Clear movie list and show progress
        self.movie_list.clear()
        progress_item = QListWidgetItem("Starting metadata rebuild...")
        self.movie_list.addItem(progress_item)
        
        # Create worker thread
        self.metadata_thread = QThread()
        self.metadata_worker = MetadataWorker(self.project_folder)
        self.metadata_worker.moveToThread(self.metadata_thread)
        
        # Connect signals
        self.metadata_thread.started.connect(self.metadata_worker.run)
        self.metadata_worker.progress.connect(self.on_metadata_progress)
        self.metadata_worker.error.connect(self.on_metadata_error)
        self.metadata_worker.finished.connect(self.on_metadata_finished)
        
        # Start thread
        self.metadata_thread.start()

    def animate_rebuild_button(self):
        """Animate the rebuilding button with dots"""
        self.rebuild_dot_count = (self.rebuild_dot_count + 1) % 4
        dots = "." * self.rebuild_dot_count
        self.metadata_button.setText(f"        Rebuilding{dots}")

    def on_metadata_progress(self, message):
        """Handle progress updates from metadata worker"""
        # Update the progress item
        if self.movie_list.count() > 0:
            item = self.movie_list.item(0)
            item.setText(message)

    def on_metadata_error(self, error_message):
        """Handle errors from metadata worker"""
        QMessageBox.critical(self, "Metadata Rebuild Error", error_message)
        self.cleanup_metadata_thread()

    def on_metadata_finished(self, success):
        """Handle completion of metadata rebuild"""
        self.cleanup_metadata_thread()
        
        if success:
            # Reload the project
            self.load_project(self.project_folder)
        else:
            QMessageBox.critical(self, "Error", "Metadata rebuild failed.")

    def cleanup_metadata_thread(self):
        """Clean up the metadata worker thread"""
        # Stop animation timer
        self.rebuild_animation_timer.stop()
        
        if self.metadata_thread:
            self.metadata_thread.quit()
            self.metadata_thread.wait()
            self.metadata_thread = None
            self.metadata_worker = None
        
        # Re-enable buttons
        self.metadata_button.setText("Rebuild Metadata")
        self.metadata_button.setEnabled(True)
        self.project_folder_button.setEnabled(True)
        
        # Reset button text alignment to center when not running
        self.metadata_button.setStyleSheet("QPushButton { text-align: center; }")

    # -------- Shotlist Bot --------

    def enable_shotlist_bot_button(self):
        # Enable only if a movie is currently selected
        self.shotlist_bot_button.setEnabled(self.selected_movie_widget is not None)

    def disable_shotlist_bot_button(self):
        self.shotlist_bot_button.setEnabled(False)

    def handle_shotlist_bot(self):
        if not self.shotlist_bot_active:
            self.shotlist_bot_active = True
            self.shotlist_bot_button.setText("      Shotlist Bot On")
            self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: left; }")
            self.shotlist_bot_anim_timer.start(500)
            # Send signal to shotlist to start detection
            self.shotlist_bot_start.emit()
        else:
            # Optionally allow stopping the bot
            self.shotlist_bot_active = False
            self.shotlist_bot_button.setText("Shotlist Bot Off")
            self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: center; }")
            self.shotlist_bot_anim_timer.stop()

    def animate_shotlist_bot(self):
        self.shotlist_bot_dots = (self.shotlist_bot_dots + 1) % 4
        dots = "." * self.shotlist_bot_dots
        self.shotlist_bot_button.setText(f"      Shotlist Bot On{dots}")
        self.shotlist_bot_button.setStyleSheet("QPushButton { text-align: left; }")

    def on_shotlist_status(self, finished):
        # Called by shotlist.py when detection is finished
        if finished and self.shotlist_bot_active:
            # Select next movie in the list
            self.select_next_movie()
        elif not finished:
            # Detection is still running, keep animating
            pass

    def select_next_movie(self):
        count = self.movie_list.count()
        # If nothing is selected but there are movies and a project folder, select the first movie
        if count > 0 and self.project_folder and not self.selected_movie_widget:
            self.turn_off_all_bots()
            first_item = self.movie_list.item(0)
            self.on_movie_clicked(first_item)
            self.scroll_to_movie(0)
            return

        if count == 0 or not self.selected_movie_widget:
            self.turn_off_all_bots()
            return
        for i in range(count):
            widget = self.movie_list.itemWidget(self.movie_list.item(i))
            if widget == self.selected_movie_widget:
                next_index = i + 1
                if next_index < count:
                    self.turn_off_all_bots()
                    next_item = self.movie_list.item(next_index)
                    self.on_movie_clicked(next_item)
                    self.scroll_to_movie(next_index)
                else:
                    self.turn_off_all_bots()
                break

    def select_previous_movie(self):
        count = self.movie_list.count()
        if count == 0 or not self.selected_movie_widget:
            return
        for i in range(count):
            widget = self.movie_list.itemWidget(self.movie_list.item(i))
            if widget == self.selected_movie_widget:
                prev_index = i - 1
                if prev_index >= 0:
                    self.turn_off_all_bots()
                    prev_item = self.movie_list.item(prev_index)
                    self.on_movie_clicked(prev_item)
                    self.scroll_to_movie(prev_index)
                break

    def scroll_to_movie(self, index):
        """Scrolls the movie list so the movie at 'index' is visible at the top if offscreen."""
        item = self.movie_list.item(index)
        if not item:
            if DEBUG:
                print(f"DEBUG: scroll_to_movie: No item at index {index}")
            return

        item_rect = self.movie_list.visualItemRect(item)
        viewport_rect = self.movie_list.viewport().rect()

        if DEBUG:
            print(f"DEBUG: scroll_to_movie called for index {index}")
            print(f"DEBUG: item_rect={item_rect}, viewport_rect={viewport_rect}")

        # If the item is not fully visible, scroll to it
        if not viewport_rect.contains(item_rect):
            if DEBUG:
                print(f"DEBUG: scrolling to movie {index} (not fully visible)")
            self.movie_list.scrollToItem(item, self.movie_list.PositionAtTop)
        else:
            if DEBUG:
                print(f"DEBUG: movie {index} already fully visible, no scroll needed.")

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        self.currently_loading_video = None
        # If bot is still active, start detection again
        if self.shotlist_bot_active:
            self.shotlist_bot_start.emit()
    
    # ---- Save/Load Preferences ----

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "project_folder": self.project_folder
        }
    
    def on_request_load(self, data):
        if "project_folder" in data and data["project_folder"]:
            # Load project folder from preferences
            folder = data["project_folder"]
            if os.path.exists(folder):
                # Don't reload if it's the same folder
                if self.project_folder != folder:
                    # Set the project folder and load it
                    self.set_project_folder(folder)
            else:
                # Folder no longer exists, reset
                self.project_folder = None
                self.project_folder_button.setText("Project Folder")
        
        # You can add any additional logic here that should happen
        # when a video finishes loading, such as:
        # - Updating UI state
        # - Logging the successful load
        # - Enabling/disabling certain features

    def handle_caption_bot(self):
        print("Caption Bot button pressed.")

# --------------------

class MovieItemWidget(QWidget):
    """Custom widget for each movie item in the list"""
    
    # Add a signal to emit when clicked
    clicked = pyqtSignal(dict)
    
    def __init__(self, movie_data, posters_folder, ui):
        super().__init__()
        self.movie_data = movie_data
        self.posters_folder = posters_folder
        self.ui = ui
        self.is_selected = False
        self.is_dark_mode = ui.is_dark_mode()

        self.setAutoFillBackground(True)

        # Create horizontal layout
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Poster label (left side)
        self.poster_label = QLabel()
        self.poster_label.setFixedSize(POSTER_WIDTH, POSTER_HEIGHT)
        self.poster_label.setStyleSheet("border: none; padding: 0px 4px 0px 0px; margin: 0px;")
        self.poster_label.setAlignment(Qt.AlignCenter)
        self.poster_label.setScaledContents(True)
        self.load_poster()
        layout.addWidget(self.poster_label)

        # Movie info (right side)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)
        info_layout.setContentsMargins(0,0,0,0)

        # Title
        title_label = QLabel(movie_data.get('title', 'Unknown Title'))
        title_label.setFont(self.ui.get_font('title'))  # Use UI font system
        title_label.setWordWrap(True)
        info_layout.addWidget(title_label)

        # Year, Director, and Duration on same line with regular font
        year = movie_data.get('year', 'Unknown Year')
        director = movie_data.get('director', 'Unknown Director')
        duration = movie_data.get('duration', '')

        # Format the duration if it exists
        if duration:
            # Assume duration is in minutes, format as "Xh Ym" or just "Ym" if under 1 hour
            try:
                duration_minutes = int(duration)
                if duration_minutes >= 60:
                    hours = duration_minutes // 60
                    minutes = duration_minutes % 60
                    duration_str = f"{hours}h {minutes}m"
                else:
                    duration_str = f"{duration_minutes}m"
            except (ValueError, TypeError):
                duration_str = str(duration)
            
            year_director_duration_text = f"{year} | {director} | {duration_str}"
        else:
            year_director_duration_text = f"{year} | {director}"

        year_director_duration_label = QLabel(year_director_duration_text)
        year_director_duration_label.setFont(self.ui.get_font('year'))
        info_layout.addWidget(year_director_duration_label)

        # Tagline
        tagline = movie_data.get('tagline', '')
        if tagline:
            tagline_label = QLabel(tagline)
            tagline_label.setWordWrap(True)
            tagline_label.setFont(self.ui.get_font('tagline'))
            info_layout.addWidget(tagline_label)
        
        info_layout.addStretch()  # Push content to top
        
        layout.addLayout(info_layout, 0)  # Give info area more space
        self.setLayout(layout)
    
    def set_selected(self, selected):
        """Set the selection state and update background"""
        self.is_selected = selected
        self.update_background()
    
    def update_background(self):
        """Update the background color based on selection state"""
        # Depending on light/dark mode, set text color
        if self.is_dark_mode:
            text_color = DARK_TEXT_COLOR
            background_color = DARK_ITEM_BACKGROUND
        else:
            text_color = LIGHT_TEXT_COLOR
            background_color = LIGHT_ITEM_BACKGROUND
        # If selected, highlight with fuchsia background
        if self.is_selected:
            self.highlight_background('white', HIGHLIGHT_COLOR)
        else:
            self.setStyleSheet(f"""
                background-color: {background_color};
                color: {text_color};
            """)
            self.setAutoFillBackground(True)
        self.repaint()

    def highlight_background(self, color, background):
        """Highlight the background with a specific color"""
        self.setStyleSheet(f"""
            MovieItemWidget {{
                background-color: {background} !important;
                color: {color} !important;
            }}
            QWidget {{
                background-color: {background} !important;
                color: {color} !important;
            }}
        """)
        self.setAutoFillBackground(True)
        self.repaint()
    
    def remove_background(self):
        """Remove background color"""
        self.setStyleSheet("""
            MovieItemWidget {
                background: transparent !important;
            }
            QWidget {
                background: transparent !important;
            }
        """)
        self.setAutoFillBackground(False)
        self.repaint()

    def mousePressEvent(self, event):
        """Handle mouse clicks on the widget"""
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.movie_data)
        super().mousePressEvent(event)

    def load_poster(self):
        """Load poster image for this movie"""
        # Get the exact filename from metadata
        filename = self.movie_data.get('filename', '')
        
        # Remove .mp4 extension if present
        if filename.endswith('.mp4'):
            filename = filename[:-4]

        # Try common image extensions
        for ext in ['.jpg', '.jpeg', '.png', '.bmp']:
            poster_path = os.path.join(self.posters_folder, f"{filename}{ext}")
            if os.path.exists(poster_path):
                pixmap = QPixmap(poster_path)
                if not pixmap.isNull():
                    self.poster_label.setPixmap(pixmap)
                    return
                else:
                    print(f"✗ Failed to load pixmap from: {poster_path}")

        # Fallback: try tmdb_id format
        tmdb_id = self.movie_data.get('tmdb_id', '')
        if tmdb_id:
            for ext in ['.jpg', '.jpeg', '.png', '.bmp']:
                poster_path = os.path.join(self.posters_folder, f"{tmdb_id}{ext}")
                if os.path.exists(poster_path):
                    pixmap = QPixmap(poster_path)
                    if not pixmap.isNull():
                        self.poster_label.setPixmap(pixmap)
                        return
                    else:
                        print(f"✗ Failed to load pixmap from fallback: {poster_path}")

        # If no poster found, show placeholder and debug info
        print(f"✗ No poster found for: {self.movie_data.get('title', 'Unknown')}")
        print(f"  Looked for: {filename}.[jpg|jpeg|png|bmp]")
        if tmdb_id:
            print(f"  Also tried: {tmdb_id}.[jpg|jpeg|png|bmp]")
        print(f"  In folder: {self.posters_folder}")
        
        self.poster_label.setText("No\nPoster")
        self.poster_label.setAlignment(Qt.AlignCenter)