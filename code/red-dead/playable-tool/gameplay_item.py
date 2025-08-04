DEBUG = True  # Set to True to enable debug output

from PyQt5.QtCore import Qt, pyqtSignal, QSize
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from PyQt5.QtGui import QPixmap
import os
from catalog_item import AbstractCatalogItemWidget, POSTER_WIDTH, POSTER_HEIGHT, ITEM_HEIGHT

THUMBNAIL_WIDTH = int(POSTER_HEIGHT * (16.0 / 9.0))
THUMBNAIL_HEIGHT = POSTER_HEIGHT

class GameplayItemWidget(AbstractCatalogItemWidget):
    """Gameplay-specific catalog item widget"""
    
    def __init__(self, gameplay_data, thumbnails_folder, ui):
        self.thumbnails_folder = thumbnails_folder
        super().__init__(gameplay_data, ui)
    
    def setup_ui(self):
        """Setup the gameplay item UI"""
        # Create horizontal layout
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)  # Add spacing between thumbnail and info

        # Thumbnail label (left side)
        self.thumbnail_label = QLabel()
        self.thumbnail_label.setFixedSize(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        self.thumbnail_label.setStyleSheet("border: none; margin: 0px;")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setScaledContents(False)  # Change to False to respect aspect ratio
        self.load_thumbnail()
        layout.addWidget(self.thumbnail_label)

        # Gameplay info (right side)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)
        info_layout.setContentsMargins(0, 4, 0, 0)  # Add top margin

        # Title
        title_label = QLabel(self.item_data.get('title', 'Unknown Title'))
        title_font = self.ui.get_font('title')
        title_font.setPointSize(12)  # Adjust title font size
        title_label.setFont(title_font)
        title_label.setWordWrap(True)
        info_layout.addWidget(title_label)

        # Duration only (no year/director for gameplay)
        duration = self.item_data.get('duration', '')
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
            
            duration_label = QLabel(duration_str)
            duration_font = self.ui.get_font('year')
            duration_font.setPointSize(12)  # Adjust duration font size
            duration_label.setFont(duration_font)
            info_layout.addWidget(duration_label)

        # Overview/description if available
        overview = self.item_data.get('overview', '')
        if overview:
            overview_label = QLabel(overview)
            overview_label.setWordWrap(True)
            overview_label.setFont(self.ui.get_font('tagline'))
            info_layout.addWidget(overview_label)

        info_layout.addStretch()  # Push content to top
        
        layout.addLayout(info_layout, 1)  # Give info area more space
        self.setLayout(layout)

    def load_thumbnail(self):
        """Load thumbnail image for this gameplay video"""
        # Get the exact filename from metadata
        filename = self.item_data.get('filename', '')
        
        if DEBUG:
            print(f"DEBUG: GameplayItemWidget: Looking for thumbnail for '{filename}'")
            print(f"DEBUG: GameplayItemWidget: Thumbnails folder: '{self.thumbnails_folder}'")
        
        # Remove .mp4 extension if present
        if filename.endswith('.mp4'):
            filename = filename[:-4]

        # Try common image extensions for thumbnails
        for ext in ['.jpg', '.jpeg', '.png', '.bmp']:
            thumbnail_path = os.path.join(self.thumbnails_folder, f"{filename}{ext}")
            if DEBUG:
                print(f"DEBUG: GameplayItemWidget: Checking thumbnail path: '{thumbnail_path}'")
            
            if os.path.exists(thumbnail_path):
                pixmap = QPixmap(thumbnail_path)
                if not pixmap.isNull():
                    if DEBUG:
                        print(f"DEBUG: GameplayItemWidget: Loaded thumbnail: '{thumbnail_path}'")
                    
                    # Scale pixmap to fit within the label while preserving aspect ratio
                    scaled_pixmap = pixmap.scaled(
                        THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT,  # Use thumbnail dimensions, not poster dimensions
                        Qt.KeepAspectRatio,      # Preserve aspect ratio
                        Qt.SmoothTransformation  # High quality scaling
                    )
                    
                    self.thumbnail_label.setPixmap(scaled_pixmap)
                    return

        # If no thumbnail found, show placeholder
        if DEBUG:
            print(f"DEBUG: GameplayItemWidget: No thumbnail found for '{filename}'")
        self.thumbnail_label.setText("No\nThumb")
        self.thumbnail_label.setAlignment(Qt.AlignCenter)
        self.thumbnail_label.setStyleSheet("border: 1px solid #666; background-color: #222; color: #888;")

    def mousePressEvent(self, event):
        """Handle mouse click events"""
        if event.button() == Qt.LeftButton:
            if DEBUG:
                print(f"DEBUG: GameplayItemWidget: Clicked on '{self.item_data.get('title', 'Unknown')}'")
            # Emit the clicked signal with the item data
            self.clicked.emit(self.item_data)
            
            # Also handle selection similar to the parent catalog system
            # This ensures the item gets selected and the signal propagates correctly
            self.set_selected(True)
        super().mousePressEvent(event)