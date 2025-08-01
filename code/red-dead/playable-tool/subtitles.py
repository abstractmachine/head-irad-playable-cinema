DEBUG = False  # Set to True to enable debug output

import os
import re
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QSizePolicy

class SubtitlesWindow(QWidget):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui  # Store UI instance

        # Set a small minimum height if desired
        self.setMinimumHeight(80)

        # Initialize variables
        self.project_folder = None
        self.current_movie_filename = None
        self.current_subtitle_path = None
        self.subtitles_data = []  # Parsed subtitle entries

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)  # Zero margins
        main_layout.setSpacing(0)                   # Zero spacing

        # Subtitles text editor (read-only display)
        self.subtitles_field = QTextEdit()
        self.subtitles_field.setPlaceholderText("")
        self.subtitles_field.setReadOnly(True)
        self.subtitles_field.setFont(self.ui.get_font('text'))
        self.subtitles_field.setStyleSheet("QTextEdit { border: none; padding: 0px; }")  # Zero border and padding
        self.subtitles_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        main_layout.addWidget(self.subtitles_field, stretch=1)

        self.setLayout(main_layout)

    def parse_srt_time(self, time_str):
        """Convert SRT time format (HH:MM:SS,mmm or HH:MM:SS.mmm) to milliseconds"""
        # Accept both ',' and '.' as millisecond separator
        if ',' in time_str:
            time_part, ms_part = time_str.split(',')
        elif '.' in time_str:
            time_part, ms_part = time_str.split('.')
        else:
            raise ValueError(f"Invalid SRT time format: '{time_str}'")
        h, m, s = map(int, time_part.split(':'))
        ms = int(ms_part)
        return (h * 3600 + m * 60 + s) * 1000 + ms

    def parse_srt_content(self, content):
        """Parse SRT file content into subtitle entries"""
        self.subtitles_data = []
        
        # Split into subtitle blocks
        blocks = re.split(r'\n\s*\n', content.strip())
        
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) < 3:
                continue
                
            try:
                # Parse subtitle number
                number = int(lines[0])
                
                # Parse time range
                time_line = lines[1]
                start_time, end_time = time_line.split(' --> ')
                start_ms = self.parse_srt_time(start_time.strip())
                end_ms = self.parse_srt_time(end_time.strip())
                
                # Parse subtitle text (can be multiple lines)
                text = '\n'.join(lines[2:])
                
                self.subtitles_data.append({
                    'number': number,
                    'start_ms': start_ms,
                    'end_ms': end_ms,
                    'text': text
                })
                
            except (ValueError, IndexError) as e:
                print(f"Error parsing subtitle block: {e}")
                continue

    def find_current_subtitle(self, current_time_ms):
        """Find the subtitle that should be displayed at the current time"""
        for subtitle in self.subtitles_data:
            if subtitle['start_ms'] <= current_time_ms <= subtitle['end_ms']:
                return subtitle
        return None

    def set_project_folder(self, project_folder):
        """Set the project folder when cinema window loads a project"""
        self.project_folder = project_folder

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        """Called when a new movie is loaded in the player"""
        movie_filename = os.path.basename(movie_path)
        
        # Don't reload if it's the same movie
        if self.current_movie_filename == movie_filename:
            return
            
        self.current_movie_filename = movie_filename
        # print(f"Subtitles: Movie loaded - {movie_filename}")
        
        if not self.project_folder:
            print("Warning: No project folder set in subtitles window")
            return
        
        # Transform filename: whatever-the-filename-is.mp4 -> whatever-the-filename-is.srt
        if movie_filename.lower().endswith('.mp4'):
            subtitle_filename = movie_filename[:-4] + '.srt'
        else:
            subtitle_filename = movie_filename + '.srt'
        
        # Set the path to the movie's subtitle file
        self.current_subtitle_path = os.path.join(self.project_folder, "subtitles", subtitle_filename)
        
        # Update window title to show current movie
        movie_name = os.path.splitext(movie_filename)[0]
        self.setWindowTitle(f"Subtitles - {movie_name}")
        
        # Load the subtitles for this movie
        self.load_movie_subtitles()

    def load_movie_subtitles(self):
        """Load the subtitles for the current movie"""
        if not self.current_subtitle_path:
            return
            
        # Check if movie-specific subtitle file exists
        if os.path.exists(self.current_subtitle_path):
            # Load existing movie subtitles
            try:
                with open(self.current_subtitle_path, "r", encoding="utf-8") as f:
                    content = f.read()
                    
                # Parse the SRT content
                self.parse_srt_content(content)
                # print(f"Loaded {len(self.subtitles_data)} subtitle entries from: {self.current_subtitle_path}")
                
                # Display initial message
                self.subtitles_field.setPlainText("")
                
            except Exception as e:
                print(f"Error loading movie subtitles: {e}")
                self.subtitles_field.setPlainText("")
                self.subtitles_data = []
        else:
            # Movie subtitles don't exist
            self.subtitles_data = []
            self.subtitles_field.setPlainText("No subtitle file found for this movie.")
            print(f"No existing subtitles found for: {self.current_movie_filename}")

    def on_timecode_changed(self, timecode_ms):
        """Called whenever the video timecode changes"""
        # Convert milliseconds to HH:MM:SS format for display
        seconds = timecode_ms // 1000
        h = seconds // 3600
        m = (seconds % 3600) // 60
        s = seconds % 60
        
        timecode_str = f"{h:02}:{m:02}:{s:02}"
        
        # Find and display current subtitle
        current_subtitle = self.find_current_subtitle(timecode_ms)
        
        if current_subtitle:
            # Display the current subtitle
            display_text = f"{current_subtitle['text']}"
            self.subtitles_field.setPlainText(display_text)
        else:
            # No subtitle at this time
            self.subtitles_field.setPlainText(f"")

    def get_subtitles_between(self, timecode_start, timecode_end):
        """Return all subtitles text between start_ms and end_ms (inclusive)."""
        start_ms = self.parse_srt_time(timecode_start)
        end_ms = self.parse_srt_time(timecode_end)
        results = []
        for sub in self.subtitles_data:
            if sub['end_ms'] < start_ms:
                continue
            if sub['start_ms'] > end_ms:
                break
            # If the subtitle overlaps the interval, include it
            if sub['start_ms'] <= end_ms and sub['end_ms'] >= start_ms:
                results.append(sub['text'])
        return "\n".join(results)

    def on_request_save(self):
        """Save window preferences"""
        geo = self.geometry()
        self._pending_save_data = {
        }
        return self._pending_save_data

    def on_request_load(self, data):
        """Load window preferences"""
        if data:
            # do nothing for now
            pass