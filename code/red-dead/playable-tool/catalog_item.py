DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QSize, QTimer
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from PyQt5.QtGui import QPixmap
import os

# Common dimensions and colors
POSTER_WIDTH = 40
POSTER_HEIGHT = 60
ITEM_HEIGHT = 63
HIGHLIGHT_COLOR = "#f0f"
DARK_ITEM_BACKGROUND = "#333"
LIGHT_ITEM_BACKGROUND = "#ddd"
DARK_TEXT_COLOR = "#fff"
LIGHT_TEXT_COLOR = "#000"

class AbstractCatalogItemWidget(QWidget):
    """Abstract base class for catalog item widgets"""
    
    # Add a signal to emit when clicked
    clicked = pyqtSignal(dict)
    
    def __init__(self, item_data, ui):
        super().__init__()
        self.item_data = item_data
        self.ui = ui
        self.is_selected = False
        self.is_dark_mode = ui.is_dark_mode()
        self.setAutoFillBackground(True)
        self.setup_ui()
    
    def setup_ui(self):
        """Setup the UI - to be overridden by subclasses"""
        raise NotImplementedError("Subclasses must implement setup_ui()")
    
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
            AbstractCatalogItemWidget {{
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
            AbstractCatalogItemWidget {
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
            self.clicked.emit(self.item_data)
        super().mousePressEvent(event)

class MovieItemWidget(AbstractCatalogItemWidget):
    """Movie-specific catalog item widget"""
    
    def __init__(self, movie_data, posters_folder, ui):
        self.posters_folder = posters_folder
        self.poster_loaded = False
        super().__init__(movie_data, ui)
    
    def setup_ui(self):
        """Setup the movie item UI"""
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
        
        # Show placeholder initially
        self.poster_label.setText("Loading...")
        self.poster_label.setAlignment(Qt.AlignCenter)
        
        # Load poster asynchronously
        QTimer.singleShot(100, self.load_poster_async)
        
        layout.addWidget(self.poster_label)

        # Movie info (right side) - same as before
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)
        info_layout.setContentsMargins(0,0,0,0)

        # Title
        title_label = QLabel(self.item_data.get('title', 'Unknown Title'))
        title_label.setFont(self.ui.get_font('title'))
        title_label.setWordWrap(True)
        info_layout.addWidget(title_label)

        # Year, Director, and Duration on same line
        year = self.item_data.get('year', 'Unknown Year')
        director = self.item_data.get('director', 'Unknown Director')
        duration = self.item_data.get('duration', '')

        # Format the duration if it exists
        if duration:
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
        tagline = self.item_data.get('tagline', '')
        if tagline:
            tagline_label = QLabel(tagline)
            tagline_label.setWordWrap(True)
            tagline_label.setFont(self.ui.get_font('tagline'))
            info_layout.addWidget(tagline_label)

        info_layout.addStretch()  # Push content to top
        
        layout.addLayout(info_layout, 0)  # Give info area more space
        self.setLayout(layout)

    def load_poster_async(self):
        """Load poster image asynchronously"""
        if self.poster_loaded:
            return
            
        # Get the exact filename from metadata
        filename = self.item_data.get('filename', '')
        
        # Remove .mp4 extension if present
        if filename.endswith('.mp4'):
            filename = filename[:-4]

        # Try common image extensions
        poster_found = False
        for ext in ['.jpg', '.jpeg', '.png', '.bmp']:
            poster_path = os.path.join(self.posters_folder, f"{filename}{ext}")
            if os.path.exists(poster_path):
                pixmap = QPixmap(poster_path)
                if not pixmap.isNull():
                    self.poster_label.setPixmap(pixmap)
                    self.poster_loaded = True
                    poster_found = True
                    break

        # Fallback: try tmdb_id format
        if not poster_found:
            tmdb_id = self.item_data.get('tmdb_id', '')
            if tmdb_id:
                for ext in ['.jpg', '.jpeg', '.png', '.bmp']:
                    poster_path = os.path.join(self.posters_folder, f"{tmdb_id}{ext}")
                    if os.path.exists(poster_path):
                        pixmap = QPixmap(poster_path)
                        if not pixmap.isNull():
                            self.poster_label.setPixmap(pixmap)
                            self.poster_loaded = True
                            poster_found = True
                            break

        # If no poster found, show placeholder
        if not poster_found:
            self.poster_label.setText("No\nPoster")
            self.poster_label.setAlignment(Qt.AlignCenter)
            self.poster_loaded = True

    def load_poster(self):
        """Legacy method - now just calls async version"""
        self.load_poster_async()