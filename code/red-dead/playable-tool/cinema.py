from PyQt5.QtCore import Qt, pyqtSignal, QSize
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QListWidget, QListWidgetItem, QLabel, QSizePolicy, 
    QFileDialog, QMessageBox
)
from PyQt5.QtGui import QPixmap, QFont, QFontDatabase
import os
import csv

# Common font size for all text
FONT_SIZE = 16

class MovieItemWidget(QWidget):
    """Custom widget for each movie item in the list"""
    
    def __init__(self, movie_data, posters_folder):
        super().__init__()
        self.movie_data = movie_data
        self.posters_folder = posters_folder
        
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

        # Get loaded fonts from CinemaWindow
        fonts = CinemaWindow.get_loaded_fonts()
        font_family = fonts.get('regular', 'Helvetica')  # Use the base family name

        # Title (using Black weight)
        title_label = QLabel(movie_data.get('title', 'Unknown Title'))
        title_font = QFont(font_family, FONT_SIZE)
        title_font.setWeight(QFont.Black)  # Set to Black weight
        title_label.setFont(title_font)
        title_label.setWordWrap(True)
        info_layout.addWidget(title_label)

        # Year and Director on same line with regular font
        year = movie_data.get('year', 'Unknown Year')
        director = movie_data.get('director', 'Unknown Director')

        year_director_label = QLabel(f"{year} <span style='color: #808080;'>|</span> {director}")
        year_director_font = QFont(font_family, FONT_SIZE)
        year_director_font.setWeight(QFont.Normal)  # Set to Normal weight
        year_director_label.setFont(year_director_font)
        info_layout.addWidget(year_director_label)

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

class CinemaWindow(QMainWindow):
    
    # Class variable to track if fonts are loaded
    _fonts_loaded = False
    _font_families = {}
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    
    def __init__(self):
        super().__init__()
        self._pending_save_data = {}
        self.setWindowTitle("Cinematheque")
        self.setGeometry(200, 200, 800, 500)
        self.project_folder = None
        
        # Load custom fonts only once
        if not CinemaWindow._fonts_loaded:
            self.load_fonts()
            CinemaWindow._fonts_loaded = True
        
        # Required project folders
        self.required_folders = ["datasets", "gameplay", "metadata", "movies", "posters", "shotlists", "subtitles"]
        
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
        layout.addWidget(self.movie_list)
        
        # Button layout
        button_layout = QHBoxLayout()
        
        # Project folder button
        self.project_folder_button = QPushButton("Project Folder")
        self.project_folder_button.clicked.connect(self.select_project_folder)
        
        # Import button (for when metadata.csv doesn't exist)
        self.import_button = QPushButton("Import Movies")
        self.import_button.clicked.connect(self.import_movies)
        self.import_button.setEnabled(False)
        
        button_layout.addWidget(self.project_folder_button)
        button_layout.addWidget(self.import_button)
        button_layout.addStretch()
        
        layout.addLayout(button_layout)
        main_widget.setLayout(layout)
        
        # Connect preference signals
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)
    
    def load_fonts(self):
        """Load custom fonts from ui/fonts/ folder"""
        # Debug the font folder path
        font_folder = os.path.join(os.path.dirname(__file__), "ui", "fonts")
        # print(f"Looking for fonts in: {font_folder}")
        # print(f"Font folder exists: {os.path.exists(font_folder)}")
        
        font_files = {
            'regular': "HKGrotesk-Regular.otf",
            'italic': "HKGrotesk-Italic.otf",
            'bold': "HKGrotesk-Bold.otf",
            'black': "HKGrotesk-Black.otf"
        }
        
        for style, font_file in font_files.items():
            font_path = os.path.join(font_folder, font_file)
            # print(f"Checking font: {font_path}")
            # print(f"Font file exists: {os.path.exists(font_path)}")
            
            if os.path.exists(font_path):
                font_id = QFontDatabase.addApplicationFont(font_path)
                if font_id != -1:
                    font_families = QFontDatabase.applicationFontFamilies(font_id)
                    if font_families:
                        CinemaWindow._font_families[style] = font_families[0]
                        # print(f"Successfully loaded {style} font: {font_families[0]}")
                    else:
                        print(f"No font families returned for: {font_file}")
                        CinemaWindow._font_families[style] = "Helvetica"
                else:
                    print(f"Failed to add font to database: {font_file}")
                    CinemaWindow._font_families[style] = "Helvetica"
            else:
                print(f"Font file not found: {font_path}")
                CinemaWindow._font_families[style] = "Helvetica"
        
        # print(f"Final font families dictionary: {CinemaWindow._font_families}")

    @classmethod
    def get_loaded_fonts(cls):
        """Return the dictionary of loaded font families"""
        # print(f"get_loaded_fonts called, returning: {cls._font_families}")
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
            return
            
        # Check if all required folders exist
        missing_folders = []
        for required_folder in self.required_folders:
            folder_path = os.path.join(folder, required_folder)
            if not os.path.exists(folder_path):
                missing_folders.append(required_folder)
        
        if missing_folders:
            # Show alert asking to create missing folders
            missing_list = ", ".join(missing_folders)
            reply = QMessageBox.question(
                self, 
                "Create Missing Folders", 
                f"The following required folders are missing:\n{missing_list}\n\nWould you like to create them?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes
            )
            
            if reply == QMessageBox.No:
                return  # User cancelled
            
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
        self.project_folder_button.setText(f"Project: {os.path.basename(folder)}")
        self.load_project(folder)
    
    def load_project(self, folder_path):
        """Called when a project folder is selected or loaded from preferences"""
        print(f"Loading project from: {folder_path}")
        
        # Check if metadata.csv exists
        metadata_path = os.path.join(folder_path, "metadata", "metadata.csv")
        
        if os.path.exists(metadata_path):
            self.load_movies_from_metadata(metadata_path, folder_path)
            self.import_button.setEnabled(False)
        else:
            # No metadata file, enable import button
            self.movie_list.clear()
            self.import_button.setEnabled(True)
            
            # Add placeholder item
            placeholder_item = QListWidgetItem("No metadata.csv found. Click 'Import Movies' to create it.")
            self.movie_list.addItem(placeholder_item)
    
    def load_movies_from_metadata(self, metadata_path, project_folder):
        """Load movies from metadata.csv file"""
        self.movie_list.clear()
        posters_folder = os.path.join(project_folder, "posters")
        
        try:
            with open(metadata_path, 'r', encoding='utf-8') as csvfile:
                reader = csv.DictReader(csvfile)
                for row in reader:
                    # Create custom widget for this movie
                    movie_widget = MovieItemWidget(row, posters_folder)
                    
                    # Create list item with fixed height
                    item = QListWidgetItem()
                    item.setSizeHint(movie_widget.size())
                    item.setSizeHint(QSize(movie_widget.width(), 140))  # Fixed height of 140px
                    
                    # Add to list
                    self.movie_list.addItem(item)
                    self.movie_list.setItemWidget(item, movie_widget)
                    
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load metadata.csv:\n{str(e)}")
    
    def import_movies(self):
        """Placeholder for movie import functionality"""
        QMessageBox.information(self, "Import Movies", "Movie import functionality will be implemented here.")
    
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
                    self.project_folder = folder
                    self.project_folder_button.setText(f"Project: {os.path.basename(folder)}")
                    self.load_project(folder)
            else:
                # Folder no longer exists, reset
                self.project_folder = None
                self.project_folder_button.setText("Project Folder")