import os
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QHBoxLayout, QVBoxLayout, QLabel
from PyQt5.QtGui import QPixmap, QFont

# Common font size for all text
FONT_SIZE = 16

class MovieItemWidget(QWidget):
    """Custom widget for each movie item in the list"""
    
    # Add a signal to emit when clicked
    clicked = pyqtSignal(dict)
    
    def __init__(self, movie_data, posters_folder):
        super().__init__()
        self.movie_data = movie_data
        self.is_selected = False
        self.setObjectName("MovieItemWidget")
        self.setAutoFillBackground(True)
        self.setFixedHeight(140)  # Fixed height for consistency
        
        # Main horizontal layout
        layout = QHBoxLayout()
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(15)
        
        # Poster (left side)
        poster_label = QLabel()
        poster_label.setFixedSize(80, 120)
        poster_label.setStyleSheet("border: 1px solid gray;")
        poster_label.setAlignment(Qt.AlignCenter)
        
        # Try to load poster image
        poster_filename = movie_data.get('filename', '').replace('.mp4', '.jpg')
        poster_path = os.path.join(posters_folder, poster_filename)
        
        if os.path.exists(poster_path):
            pixmap = QPixmap(poster_path)
            if not pixmap.isNull():
                # Scale the image to fit the label while maintaining aspect ratio
                scaled_pixmap = pixmap.scaled(80, 120, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                poster_label.setPixmap(scaled_pixmap)
            else:
                poster_label.setText("No Image")
        else:
            poster_label.setText("No Poster")
        
        layout.addWidget(poster_label)
        
        # Movie info (right side)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(0)
        info_layout.setContentsMargins(0, 10, 0, 0)  # Add top margin to push content down
        
        # Get loaded fonts from CinemaWindow
        from cinema import CinemaWindow
        fonts = CinemaWindow.get_loaded_fonts()
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
        layout.addLayout(info_layout)
        
        self.setLayout(layout)
    
    def color_widget(self, color):
        """Directly color the entire widget with the specified color"""
        if color:
            print(f"Coloring widget {self.movie_data.get('title', 'Unknown Title')} with {color}")
            # **FIX: Use QWidget instead of MovieItemWidget**
            self.setStyleSheet(f"""
                QWidget#MovieItemWidget {{
                    background-color: {color};
                    border-radius: 5px;
                }}
                QLabel {{
                    background-color: transparent;
                }}
            """)
        else:
            print(f"Removing color from widget {self.movie_data.get('title', 'Unknown Title')}")
            # **FIX: Reset but keep child transparency**
            self.setStyleSheet("""
                QLabel {
                    background-color: transparent;
                }
            """)
        self.update()  # Force repaint

    def set_selected(self, selected):
        # Set the selection state of this widget
        self.is_selected = selected
        if selected:
            self.color_widget("#ff00ff")  # Fuchsia
        else:
            self.color_widget(None)  # Remove color
    
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self.movie_data)
        super().mousePressEvent(event)