from PyQt5.QtCore import Qt, pyqtSignal, QSize, QThread, QTimer
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QSizePolicy, 
    QFileDialog, QMessageBox
)
from PyQt5.QtGui import QPixmap, QFont, QFontDatabase, QColor
import os
import csv
from metadata import MetadataWorker  # Import our metadata worker

DEBUG = True  # Set to True to enable debug messages

# Common font size for all text
FONT_SIZE = 16

class MovieItemWidget(QWidget):
    """Custom widget for each movie item in the list"""
    
    # Add a signal to emit when clicked
    clicked = pyqtSignal(dict)
    
    def __init__(self, movie_data, posters_folder):
        super().__init__()
        self.movie_data = movie_data
        self.posters_folder = posters_folder
        self.is_selected = False
        
        # Set default background
        self.setAutoFillBackground(True)
        self.update_background()
        
        # Create horizontal layout
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 0, 10, 10) # (left, top, right, bottom)
        layout.setSpacing(15)
        
        # Poster label (left side)
        self.poster_label = QLabel()
        self.poster_label.setFixedSize(80, 120)  # Poster aspect ratio
        self.poster_label.setStyleSheet("background-color: #f0f0f0; border: none;")
        self.poster_label.setAlignment(Qt.AlignCenter)
        self.poster_label.setScaledContents(True)
        
        # Load poster image if available
        self.load_poster()
        
        layout.addWidget(self.poster_label)
        
        # Movie info (right side)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)
        info_layout.setContentsMargins(0, 10, 0, 0)  # Add top margin to push content down

        # Get loaded fonts from CinemathequeWindow
        fonts = CinemathequeWindow.get_loaded_fonts()  # Updated class name
        font_family = fonts.get('regular', 'Helvetica')  # Use the base family name

        # Title (using Black weight)
        title_label = QLabel(movie_data.get('title', 'Unknown Title'))
        title_font = QFont(font_family, FONT_SIZE)
        title_font.setWeight(QFont.Black)  # Set to Black weight
        title_label.setFont(title_font)
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
        year_director_duration_font = QFont(font_family, FONT_SIZE)
        year_director_duration_font.setWeight(QFont.Normal)  # Set to Normal weight
        year_director_duration_label.setFont(year_director_duration_font)
        info_layout.addWidget(year_director_duration_label)

        # Tagline
        tagline = movie_data.get('tagline', '')
        if tagline:
            tagline_label = QLabel(tagline)
            tagline_label.setWordWrap(True)
            tagline_font = QFont(font_family, FONT_SIZE)
            tagline_font.setWeight(QFont.Normal)  # Set to Normal weight
            tagline_font.setItalic(True)  # Set italic style
            tagline_label.setFont(tagline_font)
            info_layout.addWidget(tagline_label)
        
        info_layout.addStretch()  # Push content to top
        
        layout.addLayout(info_layout, 1)  # Give info area more space
        self.setLayout(layout)
    
    def set_selected(self, selected):
        """Set the selection state and update background"""
        self.is_selected = selected
        self.update_background()
    
    def update_background(self):
        """Update the background color based on selection state"""
        if self.is_selected:
            # Use a more specific and stronger stylesheet
            self.setStyleSheet("""
                MovieItemWidget {
                    background-color: #FF00FF !important;
                }
                QWidget {
                    background-color: #FF00FF !important;
                }
            """)
            self.setAutoFillBackground(True)
        else:
            # Clear all styling completely
            self.setStyleSheet("")
            self.setAutoFillBackground(False)
        
        # Force immediate visual update
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

class CinemathequeWindow(QMainWindow):  # Updated class name
    
    # Class variable to track if fonts are loaded
    _fonts_loaded = False
    _font_families = {}
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    movie_selected = pyqtSignal(str)  # Signal to send movie file path to player
    project_loaded = pyqtSignal(str)  # Signal when project folder is loaded
    
    def __init__(self):
        super().__init__()
        self.project_folder = None
        self.currently_loading_video = None  # Track what video is currently being requested
        self.selected_movie_widget = None  # Track currently selected movie widget
        
        # Load custom fonts only once
        if not CinemathequeWindow._fonts_loaded:  # Updated class name
            self.load_fonts()
            CinemathequeWindow._fonts_loaded = True  # Updated class name
        
        # Required project folders
        self.required_folders = ["datasets", "gameplay", "metadata", "movies", "posters", "prompts", "shotlists", "subtitles"]
        
        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)
        
        # Movie list viewer
        self.movie_list = QListWidget()
        self.movie_list.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.movie_list.setAlternatingRowColors(True)
        self.movie_list.setSpacing(5)  # Add uniform spacing between items
        
        # DISABLE default Qt selection behavior
        self.movie_list.setSelectionMode(QListWidget.NoSelection)
        
        # Connect to our custom click handler only
        self.movie_list.itemClicked.connect(self.on_movie_clicked)
        layout.addWidget(self.movie_list)
        
        # Button layout
        button_layout = QHBoxLayout()

        # Project folder button
        self.project_folder_button = QPushButton("Project Folder")
        self.project_folder_button.clicked.connect(self.select_project_folder)

        # Metadata rebuild button
        self.metadata_button = QPushButton("Rebuild Metadata")
        self.metadata_button.clicked.connect(self.rebuild_metadata)
        self.metadata_button.setEnabled(False)
        self.metadata_button.setFixedSize(150, 32)  # Set fixed width and height

        button_layout.addWidget(self.project_folder_button)
        button_layout.addWidget(self.metadata_button)
        button_layout.addStretch()
        
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

    def load_fonts(self):
        """Load custom fonts from ui/fonts/ folder"""
        # Debug the font folder path
        font_folder = os.path.join(os.path.dirname(__file__), "ui", "fonts")
        
        font_files = {
            'regular': "HKGrotesk-Regular.otf",
            'italic': "HKGrotesk-Italic.otf",
            'bold': "HKGrotesk-Bold.otf",
            'black': "HKGrotesk-Black.otf"
        }
        
        for style, font_file in font_files.items():
            font_path = os.path.join(font_folder, font_file)
            
            if os.path.exists(font_path):
                font_id = QFontDatabase.addApplicationFont(font_path)
                if font_id != -1:
                    font_families = QFontDatabase.applicationFontFamilies(font_id)
                    if font_families:
                        CinemathequeWindow._font_families[style] = font_families[0]  # Updated class name
                    else:
                        print(f"No font families returned for: {font_file}")
                        CinemathequeWindow._font_families[style] = "Helvetica"  # Updated class name
                else:
                    print(f"Failed to add font to database: {font_file}")
                    CinemathequeWindow._font_families[style] = "Helvetica"  # Updated class name
            else:
                print(f"Font file not found: {font_path}")
                CinemathequeWindow._font_families[style] = "Helvetica"  # Updated class name

    @classmethod
    def get_loaded_fonts(cls):
        """Return the dictionary of loaded font families"""
        return cls._font_families
    
    def select_project_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if not folder:  # User cancelled
            return
        
        self.set_project_folder(folder)
    
    def set_project_folder(self, folder):
        """Set the project folder and check for required folders"""
        
        # Don't reload if it's the same folder
        if self.project_folder == folder:
            # EMIT THE SIGNAL EVEN IF IT'S THE SAME FOLDER
            self.project_loaded.emit(folder)
            return
            
        # Check if all required folders exist
        missing_folders = []
        for required_folder in self.required_folders:
            folder_path = os.path.join(folder, required_folder)
            if not os.path.exists(folder_path):
                missing_folders.append(required_folder)
        
        if missing_folders:
            # Show warning dialog
            message = "The following required folders are missing:\n" + "\n".join(missing_folders) + \
                      "\n\nDo you want to create them now?"
            reply = QMessageBox.question(self, "Missing Folders", message, 
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.No:
                return
            # Create missing folders
            try:
                for folder_name in missing_folders:
                    folder_path = os.path.join(folder, folder_name)
                    os.makedirs(folder_path, exist_ok=True)
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to create folders:\n{str(e)}")
                return
        
        # Set the project folder and load project
        self.project_folder = folder
        self.project_folder_button.setText(f"Project")
        self.project_loaded.emit(folder)
        self.load_project(folder)
    
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
                for row in reader:
                    # Create custom widget for this movie
                    movie_widget = MovieItemWidget(row, posters_folder)
                    
                    # Create list item with fixed height
                    item = QListWidgetItem()
                    item.setSizeHint(QSize(movie_widget.width(), 140))  # Fixed height of 140px
                    
                    # Add to list
                    self.movie_list.addItem(item)
                    self.movie_list.setItemWidget(item, movie_widget)
                    
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load metadata.csv:\n{str(e)}")

    def on_movie_clicked(self, item):
        """Handle movie item click with custom selection"""
        
        # Get the MovieItemWidget from the clicked item
        movie_widget = self.movie_list.itemWidget(item)
        if movie_widget and hasattr(movie_widget, 'movie_data'):
            
            # Clear previous selection FIRST (remove fuschia background)
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
        movie_widget.set_selected(True)
        movie_widget.update()  # Force immediate update
        self.selected_movie_widget = movie_widget
        
        movie_data = movie_widget.movie_data
        filename = movie_data.get('filename', '')
        
        if filename and self.project_folder:
            # Construct full path to movie file
            movie_path = os.path.join(self.project_folder, "movies", filename)
            
            # Check if this is the same video we're already trying to load
            if self.currently_loading_video == movie_path:
                return
            
            if os.path.exists(movie_path):
                # Set the currently loading video
                self.currently_loading_video = movie_path
                
                # Emit the signal to load the movie
                self.movie_selected.emit(movie_path)
            else:
                QMessageBox.warning(self, "File Not Found", f"Movie file not found:\n{movie_path}")

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

    def on_request_save(self):
        pos = self.pos()
        size = self.size()
        self._pending_save_data = {
            "x": pos.x(),
            "y": pos.y(),
            "width": size.width(),
            "height": size.height(),
            "project_folder": self.project_folder
        }
    
    def on_request_load(self, data):
        if "x" in data and "y" in data:
            self.move(data["x"], data["y"])
        if "width" in data and "height" in data:
            self.resize(data["width"], data["height"])
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
    
    def on_movie_loading_complete(self, movie_path):
        """Called when a movie has finished loading in the player"""
        # Reset the currently loading video tracker
        self.currently_loading_video = None
        
        # You can add any additional logic here that should happen
        # when a video finishes loading, such as:
        # - Updating UI state
        # - Logging the successful load
        # - Enabling/disabling certain features