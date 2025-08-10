DEBUG = False  # Set to True to enable debug output

# Python & OS
import os
import shutil
import re

# Qt
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, 
    QSizePolicy, QComboBox, QStackedLayout
)

class PromptWindow(QMainWindow):
    preferences_save = pyqtSignal()
    preferences_load = pyqtSignal(dict)

    def __init__(self, ui, subtitles_window=None):
        super().__init__()

        self.ui = ui
        self.subtitles_window = subtitles_window
        self.setWindowTitle("Prompt")

        self.metadata = {}
        self.row_data = {}

        if DEBUG: print("DEBUG: PromptWindow initialized")

        # Put the main layout in a vertical box layout
        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        

        # Left: vertical buttons
        button_width, button_height = self.ui.get_dimensions('button')
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        button_layout.setContentsMargins(0, 0, 0, 0)

        # These are going to be the text fields
        ilk_fields = [
            "Tags",
            "Test",
            "Prompt"
        ]

        # Create a first drop-down menu that determines what type of action
        self.prompt_ilk_dropdown = QComboBox()
        self.prompt_ilk_dropdown.addItems(ilk_fields)
        self.prompt_ilk_dropdown.setCurrentIndex(0)
        self.prompt_ilk_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_ilk_dropdown.setFixedHeight(button_height)
        self.prompt_ilk_dropdown.setMinimumWidth(110)
        self.prompt_ilk_dropdown.setMaximumWidth(130)

        # Map prompt types to filenames
        self.prompt_file_map = {
            "System": "system.txt",
            "Goal": "goal.txt", 
            "Format": "format.txt",
            "Warning": "warning.txt",
            "Context": "context.txt",
            "Shot": "shot.txt",
            "Scene": "scene.txt",
            "Gameplay": "gameplay.txt",
            "Image": "image.txt",
            "Experimental": "experimental.txt"
        }

        # These are going to be the text fields
        self.text_fields = {
            "Tags": None,
            "Test": None,
            "System": None,
            "Shot": None,
            "Scene": None,
            "Gameplay": None,
            "Goal": None,
            "Format": None,
            "Warning": None,
            "Context": None,
            "Image": None,
            "Experimental": None
        }

        # Dropdown Menu
        self.prompt_name_dropdown = QComboBox()
        # use self.text_fields.keys() to populate the dropdown
        self.prompt_name_dropdown.addItems(self.text_fields.keys())
        self.prompt_name_dropdown.setCurrentIndex(0)  # Default to "System"
        
        # Connect both signals for different behaviors
        self.prompt_name_dropdown.currentIndexChanged.connect(self.handle_prompt_name_changed)
        self.prompt_name_dropdown.activated.connect(self.handle_prompt_name_activated)

        # UI Stuff
        self.prompt_name_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_name_dropdown.setFixedHeight(button_height)
        self.prompt_name_dropdown.setMinimumWidth(110)
        self.prompt_name_dropdown.setMaximumWidth(130)

        button_layout.addWidget(self.prompt_ilk_dropdown)
        button_layout.addWidget(self.prompt_name_dropdown)
        button_layout.addStretch()

        main_layout.addLayout(button_layout, stretch=0)

        # Stacked Layout

        text_layout = QVBoxLayout()
        text_layout.setSpacing(0)
        text_layout.setContentsMargins(0, 0, 0, 0)

        # We will use a stacked layout to switch between text fields
        self.stacked_layout = QStackedLayout()

        # Text Fields

        # Loop through text fields and create QTextEdit for each
        for field_name in self.text_fields.keys():
            text_edit = QTextEdit()
            text_edit.setReadOnly(False)  # Make all fields editable by default
            text_edit.setFont(self.ui.get_font('prompt'))
            text_edit.setStyleSheet("QTextEdit { border: none; padding: 0px; }")  # No border, no padding
            self.stacked_layout.addWidget(text_edit)
            self.text_fields[field_name] = text_edit
            # When a field is edited, save the current prompt, passing the field name
            text_edit.textChanged.connect(lambda name=field_name: self.save_current_prompt(name))

        # Test and Tags fields are read-only
        self.text_fields["Test"].setReadOnly(True)
        self.text_fields["Tags"].setReadOnly(True)

        text_layout.addLayout(self.stacked_layout)
        main_layout.addLayout(text_layout, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Project folder
        self.project_folder = None
        self.prompt_type = None

        # Set dropdown to none selected initially
        self.prompt_name_dropdown.setCurrentIndex(-1)

    def handle_prompt_name_activated(self, idx):
        """Handle prompt name activation (including re-selection of same item)."""
        if DEBUG: print(f"DEBUG: Prompt name activated at index {idx}")

        # Simply call the existing handler - it will handle all the logic
        self.handle_prompt_name_changed(idx)

    def handle_prompt_name_changed(self, idx):
        """Handle prompt name change from dropdown."""

        # Handle unselected case
        if idx == -1:
            self.prompt_type = None
            if DEBUG: print(f"DEBUG: Prompt type changed to unselected")
            return

        # Update prompt type based on selection
        self.prompt_type = self.prompt_name_dropdown.itemText(idx)
        if DEBUG: print(f"DEBUG: Prompt type changed to {self.prompt_type}")

        # Set the index of the stacked layout to match the selected prompt type
        self.stacked_layout.setCurrentIndex(idx)
        
        # Switch stacked layout based on selection
        if self.prompt_type == "Test":
            # Auto-run test when switching to test view
            self.handle_test_button()
        elif self.prompt_type == "Tags":
            # Load the tags cheatsheet when switching to Tags view
            self.load_tags()
        else:
            # Load the prompt for the selected type
            self.load_prompt()

    def handle_row_data(self, row_data):
        """Handle row data emitted from shotlist."""
        self.row_data = row_data or {}
        if DEBUG: print(f"DEBUG Prompt: Received row data: {len(self.row_data)} items")

    def handle_test_button(self):
        """Test the current system prompt parsing."""

        # Clear previous test output
        self.text_fields["Test"].clear()
        # Check if we have a project folder set
        if not self.project_folder:
            self.text_fields["Test"].setPlainText("ERROR: No project folder set")
            return
        
        # Parse the system prompt (function now handles prompts directory internally)
        parsed_prompt = parse_system_prompt_files(
            self.project_folder,
            getattr(self, 'current_metadata', self.metadata),
            self.row_data,
            self.subtitles_window
        )
        
        if parsed_prompt == "":
            self.text_fields["Test"].setPlainText("ERROR: Prompt parsing failed. Check console for recursion errors.")
        else:
            self.text_fields["Test"].setPlainText(parsed_prompt)

    def load_tags(self):
        """Load the cheatsheet for prompt tags."""
        if not self.project_folder:
            self.text_fields["Tags"].setPlainText("ERROR: No project folder set")
            return
        
        prompts_dir = os.path.join(self.project_folder, "prompts")
        tags_path = os.path.join(prompts_dir, "tags.txt")

        if not os.path.exists(tags_path):
            self.text_fields["Tags"].setPlainText("ERROR: Tags file not found")
            return
        
        try:
            with open(tags_path, 'r', encoding='utf-8') as f:
                tags_content = f.read()
            self.text_fields["Tags"].setPlainText(tags_content)
        except Exception as e:
            self.text_fields["Tags"].setPlainText(f"ERROR: Could not read tags: {e}")

    def handle_row_did_change(self, row_data):
        """Handle row data change emitted from shotlist."""
        # if the prompt type is Test, re-run the test with new row data
        if self.prompt_type == "Test":
            if DEBUG: print("DEBUG Prompt: Row data changed, re-running test")
            self.handle_test_button()

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        if DEBUG: print(f"DEBUG Prompt: on_movie_loaded called with {movie_path}")
        self.current_metadata = metadata
        
        # If Test is currently selected, re-run the test with new metadata
        if self.prompt_type == "Test":
            if DEBUG: print("DEBUG Prompt: Test is selected, re-running test with new metadata")
            self.handle_test_button()

    def load_prompt(self):
        """Load the appropriate prompt file based on current prompt type."""
        if DEBUG: print(f"DEBUG: load_prompt called for {self.prompt_type}")
        # make sure we have a project folder and prompt type set
        if not self.project_folder or not self.prompt_type:
            if DEBUG: print("DEBUG: No project folder or prompt type set, cannot load prompt")
            return
        # Check if the prompt type is valid
        if self.prompt_type not in self.prompt_file_map:
            if DEBUG: print(f"DEBUG: Invalid prompt type: {self.prompt_type}")
            return

        filename = self.prompt_file_map[self.prompt_type]
        file_path = os.path.join(self.prompts_directory, filename)
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.text_fields[self.prompt_type].setPlainText(content)
            if DEBUG: print(f"DEBUG: Loaded {filename}")
        except Exception as e:
            if DEBUG: print(f"DEBUG: Could not load {filename}: {e}")
            self.text_fields[self.prompt_type].setPlainText(f"ERROR: Could not load {filename}")

    def save_current_prompt(self, field_name):
        """Save the current prompt field to its corresponding file."""

        if DEBUG: print(f"DEBUG: save_current_prompt called for {field_name}")
        
        if not self.project_folder or field_name in ["Test", "Tags"]:
            return  # Don't save Test and Tags fields

        if field_name not in self.prompt_file_map:
            return

        filename = self.prompt_file_map[field_name]
        file_path = os.path.join(self.prompts_directory, filename)
        text = self.text_fields[field_name].toPlainText()
        
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(text)
            if DEBUG: print(f"DEBUG: Saved {filename}")
        except Exception as e:
            if DEBUG: print(f"DEBUG: Could not save {filename}: {e}")

    # Preferences handling
    def on_preferences_save(self):
        geo = self.geometry()
        if DEBUG: print(f"DEBUG: on_preferences_save called, geometry={geo}")
        self._pending_save_data = {
        }
        return self._pending_save_data

    # Preferences handling
    def on_preferences_load(self, data):
        if DEBUG: print(f"DEBUG: on_preferences_load called with {data}")
        if data:
            # do nothing
            pass

    def on_project_folder_loaded(self, project_folder):
        """Set the current project folder and update paths for prompt files."""
        self.project_folder = project_folder

        # List of required files
        required_files = [
            'context.txt', 'experimental.txt', 'format.txt', 'gameplay.txt', 'goal.txt', 'image.txt', 'scene.txt', 'shot.txt', 'system.txt', 'tags.txt', 'warning.txt'
        ]

        # Preferences source directory (now in prompts subdirectory)
        preferences_prompts_dir = os.path.join(os.path.dirname(__file__), "preferences", "prompts")

        # Ensure prompts subfolder exists
        self.prompts_directory = os.path.join(self.project_folder, "prompts")
        os.makedirs(self.prompts_directory, exist_ok=True)
        if DEBUG: print(f"DEBUG: Prompts directory set to {self.prompts_directory}")

        # Update paths for each required file and copy if missing
        for filename in required_files:
            dest_path = os.path.join(self.prompts_directory, filename)
            src_path = os.path.join(preferences_prompts_dir, filename)
            if not os.path.exists(dest_path) and os.path.exists(src_path):
                shutil.copyfile(src_path, dest_path)
                if DEBUG: print(f"DEBUG: Copied {filename} to {dest_path}")

# ----------------------------------------------------------------

# Function to parse system prompt files recursively

def parse_system_prompt_files(project_directory, metadata=None, row_data=None, subtitles_window=None):
    """
    Parse system prompt by processing system.txt and recursively resolving file tags.
    
    Args:
        project_directory: Path to project directory (will look for prompts/ subdirectory)
        metadata: Movie metadata dict
        row_data: Current shot data
        subtitles_window: Reference to subtitles window
    
    Returns:
        str: Parsed system prompt or empty string if recursion detected
    """
    if metadata is None:
        metadata = {}
    if row_data is None:
        row_data = {}
    
    # Determine prompts directory from project directory
    prompts_directory = os.path.join(project_directory, "prompts")
    
    # Check if prompts directory exists
    if not os.path.exists(prompts_directory):
        print(f"ERROR: Prompts directory not found at {prompts_directory}")
        return ""
    
    def read_file_safe(filename):
        """Safely read a file, return empty string if not found."""
        try:
            filepath = os.path.join(prompts_directory, filename)
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            if DEBUG: print(f"DEBUG: Could not read {filename}: {e}")
            return ""
    
    def parse_recursive(text, calling_stack=None):
        """
        Recursively parse text, resolving file tags and metadata tags.
        
        Args:
            text: Text to parse
            calling_stack: List of files currently being processed (for recursion detection)
        
        Returns:
            str: Parsed text or empty string if recursion detected
        """
        if calling_stack is None:
            calling_stack = []
        
        def replace_tag(match):
            tag = match.group(1)
            
            # Handle file tags (e.g., {goal-prompt}, {format-prompt})
            if tag.endswith('-prompt'):
                base_name = tag[:-7]  # Remove '-prompt' suffix
                filename = f"{base_name}.txt"
                
                # Check for recursion
                if filename in calling_stack:
                    print(f"ERROR: Recursion detected in prompt parsing!")
                    print(f"Calling stack: {' -> '.join(calling_stack)} -> {filename}")
                    return ""  # This will cause the entire parsing to fail
                
                # Read file and parse recursively
                file_content = read_file_safe(filename)
                if file_content:
                    new_stack = calling_stack + [filename]
                    parsed_content = parse_recursive(file_content, new_stack)
                    if parsed_content == "" and file_content != "":
                        # Recursion was detected in nested parsing
                        return ""
                    return parsed_content
                else:
                    return f"[FILE NOT FOUND: {filename}]"
            
            # Handle metadata tags
            elif tag in metadata:
                return str(metadata[tag])
            
            # Handle special tags
            elif tag == "shot-subtitles":
                if row_data and subtitles_window:
                    try:
                        timecode_start = row_data.get('Start', '')
                        timecode_end = row_data.get('End', '')
                        if timecode_start and timecode_end:
                            return subtitles_window.get_subtitles_between(timecode_start, timecode_end)
                    except Exception as e:
                        if DEBUG: print(f"DEBUG: Error getting subtitles: {e}")
                return ""
            
            elif tag == "scene-subtitles":
                # TODO: Implement scene subtitle extraction
                return "[SCENE SUBTITLES NOT YET IMPLEMENTED]"
            
            elif tag == "movie-subtitles":
                # Extract all subtitles from the entire movie
                if subtitles_window:
                    try:
                        return subtitles_window.get_all_subtitles()
                    except Exception as e:
                        if DEBUG: print(f"DEBUG: Error getting all movie subtitles: {e}")
                        return "[ERROR RETRIEVING MOVIE SUBTITLES]"
                return "[NO SUBTITLES WINDOW AVAILABLE]"
            
            elif tag == "image-count":
                return str(metadata.get('image-count', '0'))
            
            # Handle direct file inclusion (e.g., {goal.txt})
            elif tag.endswith('.txt'):
                filename = tag
                
                # Check for recursion
                if filename in calling_stack:
                    print(f"ERROR: Recursion detected in prompt parsing!")
                    print(f"Calling stack: {' -> '.join(calling_stack)} -> {filename}")
                    return ""
                
                # Read file and parse recursively
                file_content = read_file_safe(filename)
                if file_content:
                    new_stack = calling_stack + [filename]
                    parsed_content = parse_recursive(file_content, new_stack)
                    if parsed_content == "" and file_content != "":
                        # Recursion was detected in nested parsing
                        return ""
                    return parsed_content
                else:
                    if DEBUG: print(f"DEBUG: File {filename} not found in prompts directory")
                    return f"[FILE NOT FOUND: {filename}]"


            else: # Unknown tag
                if DEBUG: print(f"DEBUG: Unknown tag: {tag}")
                return_text = "{" + tag + "}"
                return return_text  # Return the tag as is
        
        # Replace all tags in the text
        result = re.sub(r"\{([a-zA-Z0-9_.-]+)\}", replace_tag, text)
        
        # Check if any recursion was detected (empty string returned from nested call)
        if "" in [match.group() for match in re.finditer(r"\{([a-zA-Z0-9_.-]+)\}", text)] and result == "":
            return ""
        
        return result
    
    # Start parsing with system.txt
    system_content = read_file_safe("system.txt")
    if not system_content:
        print("ERROR: system.txt not found or empty")
        print("Prompts directory:", prompts_directory)
        return ""
    
    try:
        parsed_result = parse_recursive(system_content, ["system.txt"])
        if parsed_result == "" and system_content != "":
            print("ERROR: Parsing failed due to recursion or other error")
            return ""
        return parsed_result
    except Exception as e:
        print(f"ERROR: Exception during prompt parsing: {e}")
        return ""