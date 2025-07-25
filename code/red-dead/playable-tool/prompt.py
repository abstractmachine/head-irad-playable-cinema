import os
from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, QPushButton, 
    QLabel, QSizePolicy
)

class PromptWindow(QMainWindow):
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Prompt")
        
        # Initialize variables
        self.project_folder = None
        self.current_movie_filename = None
        self.current_prompt_path = None
        
        # Main layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(10)

        # System prompt editor
        self.system_prompt_field = QTextEdit()
        self.system_prompt_field.setPlaceholderText("No movie loadaded")
        
        # Load custom font
        font_path = os.path.join(os.path.dirname(__file__), "ui/fonts/HKGrotesk-Regular.otf")
        font_id = QFontDatabase.addApplicationFont(font_path)
        font_families = QFontDatabase.applicationFontFamilies(font_id)
        if font_families:
            hk_font = QFont(font_families[0], 12)
        else:
            hk_font = QFont("Helvetica", 12)
        
        self.system_prompt_field.setFont(hk_font)
        self.system_prompt_field.setStyleSheet("QTextEdit { border: none; padding: 5px; }")
        main_layout.addWidget(self.system_prompt_field, stretch=1)

        # Set up container
        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Default prompt file path
        self.default_prompt_path = os.path.join(
            os.path.dirname(__file__),
            "preferences",
            "default_prompt.txt"
        )
        
        # DON'T load any text initially - wait for a movie to be loaded
        # self.load_default_prompt()  # REMOVED

        # Auto-save on every character change
        self.system_prompt_field.textChanged.connect(self.save_current_prompt)

    def set_project_folder(self, project_folder):
        """Set the project folder when cinema window loads a project"""
        self.project_folder = project_folder

    def on_movie_loaded(self, movie_path):
        """Called when a new movie is loaded in the player"""
        
        if not self.project_folder:
            print("Warning: No project folder set in prompt window")
            return
            
        # Extract filename from path
        movie_filename = os.path.basename(movie_path)
        
        # Don't reload if it's the same movie
        if self.current_movie_filename == movie_filename:
            return
            
        self.current_movie_filename = movie_filename
        
        # Transform filename: whatever-the-filename-is.mp4 -> whatever-the-filename-is.txt
        if movie_filename.lower().endswith('.mp4'):
            prompt_filename = movie_filename[:-4] + '.txt'
        else:
            prompt_filename = movie_filename + '.txt'
        
        # Set the path to the movie's prompt file
        self.current_prompt_path = os.path.join(self.project_folder, "prompts", prompt_filename)
        
        # Update window title to show current movie
        movie_name = os.path.splitext(movie_filename)[0]
        self.setWindowTitle(f"Prompt - {movie_name}")
        
        # Load the prompt for this movie
        self.load_movie_prompt()

    def load_movie_prompt(self):
        """Load the prompt for the current movie"""
        if not self.current_prompt_path:
            return
            
        # Check if movie-specific prompt file exists
        if os.path.exists(self.current_prompt_path):
            # Load existing movie prompt
            try:
                with open(self.current_prompt_path, "r", encoding="utf-8") as f:
                    text = f.read()
                self.system_prompt_field.setPlainText(text)
            except Exception as e:
                print(f"Error loading movie prompt: {e}")
                self.load_default_prompt()
        else:
            # Movie prompt doesn't exist, create it from default prompt
            self.create_movie_prompt_from_default()

    def create_movie_prompt_from_default(self):
        """Create a new movie prompt file from the default prompt"""
        try:
            # Load default prompt
            default_text = ""
            if os.path.exists(self.default_prompt_path):
                with open(self.default_prompt_path, "r", encoding="utf-8") as f:
                    default_text = f.read()
            else:
                default_text = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            
            # Set text in editor
            self.system_prompt_field.setPlainText(default_text)
            
            # Save it as the movie's prompt file
            self.save_current_prompt()
            
        except Exception as e:
            print(f"Error creating movie prompt from default: {e}")
            self.load_default_prompt()

    def load_default_prompt(self):
        """Load default prompt (fallback or when no movie is loaded)"""
        try:
            with open(self.default_prompt_path, "r", encoding="utf-8") as f:
                text = f.read()
            self.system_prompt_field.setPlainText(text)
        except Exception as e:
            default_prompt = "Analyze these video frames and provide a detailed description of the scene, focusing on visual elements, actions, and composition."
            self.system_prompt_field.setPlainText(default_prompt)
            print(f"Could not load default prompt: {e}")

    def save_current_prompt(self):
        """Save the current prompt to the appropriate file"""
        text = self.system_prompt_field.toPlainText()
        
        # Only save if we have a movie loaded and a valid prompt path
        if self.current_prompt_path and self.project_folder:
            # Save to movie-specific prompt file
            try:
                # Ensure prompts directory exists
                prompts_dir = os.path.dirname(self.current_prompt_path)
                os.makedirs(prompts_dir, exist_ok=True)
                
                with open(self.current_prompt_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                print(f"Error saving movie prompt: {e}")
        # REMOVED: No fallback saving to default_prompt.txt
        # If no movie is loaded, don't save anywhere

    def on_request_save(self):
        """Save window preferences"""
        geo = self.geometry()
        self._pending_save_data = {
            "x": geo.x(),
            "y": geo.y(),
            "width": geo.width(),
            "height": geo.height()
        }
        return self._pending_save_data

    def on_request_load(self, data):
        """Load window preferences"""
        if data:
            x = data.get("x", 100)
            y = data.get("y", 100)
            w = data.get("width", 600)
            h = data.get("height", 400)
            self.setGeometry(x, y, w, h)