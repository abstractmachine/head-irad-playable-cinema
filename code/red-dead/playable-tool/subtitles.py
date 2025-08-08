DEBUG = False  # Set to True to enable debug output
ERROR = True  # Set to True to enable error output

import os
import re
from PyQt5.QtCore import Qt, pyqtSignal, QThread, pyqtSignal
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QTextEdit, QSizePolicy
from utility import timecode_to_milliseconds, milliseconds_to_timecode

# A worker thread to handle importing the subtitles from the project folder
class SubtitlesImportWorker(QThread):
    def __init__(self, folder_path, subtitles_db):
        super().__init__()
        self.folder_path = folder_path
        self.subtitles_db = subtitles_db

    def run(self):
        subtitles_dir = os.path.join(self.folder_path, "subtitles")
        if os.path.exists(subtitles_dir):
            srt_files = [
                f for f in os.listdir(subtitles_dir)
                if f.lower().endswith('.srt') and not f.startswith('.')
            ]
            for srt_file in srt_files:
                # remove the .srt extension by removing the last 4 characters
                movie_name = srt_file[:-4]
                subtitle_path = os.path.join(subtitles_dir, srt_file)
                # get the file content
                try:
                    with open(subtitle_path, 'r', encoding='utf-8') as f:
                        # read the file content
                        file_content = f.read()
                        # create a new entry in the subtitles database
                        self.subtitles_db[movie_name] = self.parse_subtitle(srt_file, file_content)
                except Exception as e:
                    print(f"Error reading subtitle file {srt_file}: {e}")
                    continue
                
            # error handling if no subtitle files found
            if not srt_files:
                print(f"No subtitle files found in {subtitles_dir}")
        # error handling if 'subtitles' folder is not found
        else:
            print(f"No 'subtitles' folder found in {self.folder_path}")

    def parse_subtitle(self, srt_file, content):
        """Parse SRT file content into subtitle entries"""
        subtitles_data = []
        blocks = re.split(r'\n\s*\n', content.strip())
        # go through each block
        for block in blocks:
            lines = block.strip().split('\n')
            if len(lines) < 3:
                # if DEBUG: print(f"DEBUG: Skipping block with insufficient lines: {lines}")
                continue
            try:
                number_line = lines[0].lstrip('\ufeff').strip()
                if not number_line.isdigit():
                    continue
                number = int(number_line)
                time_line = lines[1]
                start_time, end_time = time_line.split(' --> ')
                start_ms = timecode_to_milliseconds(start_time.strip())
                end_ms = timecode_to_milliseconds(end_time.strip())
                if start_ms is None or end_ms is None:
                    if DEBUG: print(f"DEBUG: Skipping block with invalid timecodes: {block}")
                    continue
                text = '\n'.join(lines[2:])
                subtitles_data.append({
                    'number': number,
                    'start_ms': start_ms,
                    'end_ms': end_ms,
                    'text': text
                })
                # if DEBUG: print(f"DEBUG: Parsed subtitle #{number}: {start_ms}-{end_ms} '{text[:30]}'")
            except (ValueError, IndexError) as e:
                print(f"Error parsing {srt_file} subtitle block: {e}")
                if DEBUG: print(f"DEBUG: Block parse error in {srt_file}: {block}")
                continue
        return subtitles_data

class SubtitlesWindow(QWidget):
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)

    def __init__(self, ui):
        super().__init__()
        self.ui = ui  # Store UI instance

        # Initialize variables
        self.project_folder = None
        self.current_movie_filename = None
        self.current_subtitle_path = None
        self.subtitles_data = []  # Parsed subtitle entries
        self.subtitles_loaded = False

        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Subtitles text editor (read-only display)
        self.subtitles_field = QTextEdit()
        self.subtitles_field.setPlaceholderText("")
        self.subtitles_field.setReadOnly(True)
        self.subtitles_field.setFont(self.ui.get_font('text'))
        self.subtitles_field.setStyleSheet("QTextEdit { border: none; padding: 0px; }")  # Zero border and padding
        self.subtitles_field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        main_layout.addWidget(self.subtitles_field, stretch=1)

        self.setLayout(main_layout)

    def find_current_subtitle(self, current_time_ms):
        """Find the subtitle that should be displayed at the current time"""
        for subtitle in self.subtitles_data:
            # Defensive: skip malformed entries
            start_ms = subtitle.get('start_ms')
            end_ms = subtitle.get('end_ms')
            if start_ms is None or end_ms is None:
                continue
            if start_ms <= current_time_ms <= end_ms:
                # if DEBUG:
                #     print(f"DEBUG: MATCH subtitle #{subtitle.get('number', '?')} for time {current_time_ms}")
                return subtitle
        return None

    def set_project_folder(self, project_folder):
        """Set the project folder when cinema window loads a project"""
        self.project_folder = project_folder
        # reset flag
        self.subtitles_loaded = False
        # create subtitles database
        self.subtitles_db = {}  # Clear previous cache
        # Start worker thread to print the path
        self.worker = SubtitlesImportWorker(project_folder, subtitles_db=self.subtitles_db)
        self.worker.start()
        self.worker.finished.connect(self.subtitles_finished_loading)

    def subtitles_finished_loading(self):
        """Called when subtitles have finished loading"""
        self.subtitles_loaded = True

    def clear_project(self):
        """Clear project - for consistency with other windows"""
        # A Placeholder for future functionality
        if DEBUG: print("DEBUG: ProjectWindow: clear_project called (no action needed)")
        
    def on_movie_loaded(self, movie_path, metadata):
        """Called when a new movie is loaded in the player"""
        # reset the subtitles data
        self.subtitles_data = []
        # extract the movie name from the path by removing the extension
        movie_name = os.path.splitext(os.path.basename(movie_path))[0]
        # find the subtitles for this movie in the subtitles database
        if movie_name in self.subtitles_db:
            self.subtitles_data = self.subtitles_db[movie_name]
            if DEBUG: print(f"DEBUG: Subtitles loaded for movie '{movie_name}' with {len(self.subtitles_data)} entries")
        else:
            if DEBUG or ERROR: print(f"DEBUG: No subtitles found for movie '{movie_name}'")
            self.subtitles_data = []

    def on_timecode_changed(self, timecode_ms):
        """Called whenever the video timecode changes"""

        # first make sure we have subtitles loaded
        if not self.subtitles_loaded:
            if DEBUG: print("DEBUG: Subtitles not loaded yet, skipping timecode update")
            return
        
        timecode_str = milliseconds_to_timecode(timecode_ms)
        if not timecode_str:
            if DEBUG: print("DEBUG: Invalid timecode, skipping update")
            return

        # Find the current subtitle based on the timecode
        current_subtitle = self.find_current_subtitle(timecode_ms)

        # if DEBUG: print(f"DEBUG: Timecode changed: {timecode_ms} ({timecode_str})")
        if current_subtitle:
            display_text = f"{current_subtitle['text']}"
            self.subtitles_field.setPlainText(display_text)
            # if DEBUG: print(f"DEBUG: Displaying subtitle: {display_text[:30]}")
        else:
            self.subtitles_field.setPlainText(f"")
            # if DEBUG: print("DEBUG: No subtitle at this time")

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

    def get_all_subtitles(self):
        """Return all subtitle text from the entire movie as a single string."""
        if not self.subtitles_data:
            return ""
        
        # Extract all subtitle text and join with newlines
        all_text = []
        for subtitle in self.subtitles_data:
            all_text.append(subtitle['text'])
        
        return "\n".join(all_text)

    def on_preferences_save(self):
        """Save window preferences"""
        geo = self.geometry()
        self._pending_save_data = {
        }
        return self._pending_save_data

    def on_preferences_load(self, data):
        """Load window preferences"""
        if data:
            # do nothing for now
            pass