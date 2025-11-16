DEBUG = False  # Set to True to enable debug output

# Python & OS
import os
import shutil
import re

# Qt
from PyQt5.QtCore import pyqtSignal
from PyQt5.QtWidgets import (
    QMainWindow, QVBoxLayout, QHBoxLayout, QWidget, QTextEdit, 
    QComboBox, QStackedLayout
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
        
        # Project folder
        self.project_folder = None
        self.action = None
        self.source = None
        self.list_type = None
        self.prompt_name = None

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

        # Create a first drop-down menu that determines what type of action
        self.prompt_action_dropdown = QComboBox()
        self.prompt_action_dropdown.addItem("Tags")
        self.prompt_action_dropdown.addItem("Test")
        self.prompt_action_dropdown.addItem("Prompt")
        self.prompt_action_dropdown.setCurrentIndex(0)
        self.prompt_action_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_action_dropdown.setFixedHeight(button_height)
        self.prompt_action_dropdown.setMaximumWidth(130)
        self.prompt_action_dropdown.currentIndexChanged.connect(self.handle_prompt_action_changed)

        self.prompt_sources_dropdown = QComboBox()
        self.prompt_sources_dropdown.addItem("Play")
        self.prompt_sources_dropdown.addItem("Movie")
        self.prompt_sources_dropdown.setCurrentIndex(0)
        self.prompt_sources_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_sources_dropdown.setFixedHeight(button_height)
        self.prompt_sources_dropdown.setMaximumWidth(130)
        self.prompt_sources_dropdown.currentIndexChanged.connect(self.handle_prompt_source_changed)

        self.prompt_list_type_dropdown = QComboBox()
        self.prompt_list_type_dropdown.addItem("System")
        self.prompt_list_type_dropdown.addItem("Shot")
        self.prompt_list_type_dropdown.addItem("Scene")
        self.prompt_list_type_dropdown.addItem("Experiment")
        self.prompt_list_type_dropdown.setCurrentIndex(0)
        self.prompt_list_type_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_list_type_dropdown.setFixedHeight(button_height)
        self.prompt_list_type_dropdown.setMaximumWidth(130)
        self.prompt_list_type_dropdown.currentIndexChanged.connect(self.handle_prompt_list_type_changed)

        # These are going to be the text fields
        self.text_fields = {
            "tags": None,
            "test": None,
            "play-system": None,
            "movie-system": None,
            "play-shot": None,
            "movie-shot": None,
            "play-scene": None,
            "movie-scene": None,
            "goal": None,
            "format": None,
            "warning": None,
            "context": None,
            "image": None,
            "experiment": None
        }

        # Map text fields to filenames
        self.prompt_file_map = {
            "play-system": "play-system.txt",
            "play-shot": "play-shot.txt",
            "play-scene": "play-scene.txt",
            "movie-system": "movie-system.txt",
            "movie-shot": "movie-shot.txt",
            "movie-scene": "movie-scene.txt",
            "experiment": "experiment.txt",
            "goal": "goal.txt", 
            "format": "format.txt",
            "warning": "warning.txt",
            "context": "context.txt",
            "image": "image.txt"
        }

        # Dropdown Menu
        self.prompt_name_dropdown = QComboBox()
        self.prompt_name_dropdown.addItem("System")
        self.prompt_name_dropdown.addItem("Shot")
        self.prompt_name_dropdown.addItem("Scene")
        self.prompt_name_dropdown.addItem("Goal")
        self.prompt_name_dropdown.addItem("Format")
        self.prompt_name_dropdown.addItem("Warning")
        self.prompt_name_dropdown.addItem("Context")
        self.prompt_name_dropdown.addItem("Image")
        self.prompt_name_dropdown.addItem("Experiment")
        self.prompt_name_dropdown.setFont(self.ui.get_font('button'))
        self.prompt_name_dropdown.setFixedHeight(button_height)
        self.prompt_name_dropdown.setMinimumWidth(110)
        self.prompt_name_dropdown.setMaximumWidth(130)
        self.prompt_name_dropdown.activated.connect(self.handle_prompt_name_activated)

        # Default starting states

        self.prompt_action_dropdown.setCurrentIndex(-1)
        self.prompt_action_dropdown.setEnabled(True)
        self.prompt_sources_dropdown.setCurrentIndex(-1)
        self.prompt_sources_dropdown.setEnabled(False)
        self.prompt_list_type_dropdown.setCurrentIndex(-1)
        self.prompt_list_type_dropdown.setEnabled(False)
        self.prompt_name_dropdown.setEnabled(False)
        self.prompt_name_dropdown.setCurrentIndex(-1)

        button_layout.addWidget(self.prompt_action_dropdown)
        button_layout.addWidget(self.prompt_sources_dropdown)
        button_layout.addWidget(self.prompt_list_type_dropdown)
        button_layout.addWidget(self.prompt_name_dropdown)
        button_layout.addStretch()

        main_layout.addLayout(button_layout, stretch=0)

        # Stacked Layout

        text_layout = QVBoxLayout()
        text_layout.setSpacing(0)
        text_layout.setContentsMargins(0, 0, 0, 0)

        # We will use a stacked layout to switch between text fields
        self.stacked_layout = QStackedLayout()
        self.stacked_layout.addWidget(QWidget())

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

        # test and tags fields are read-only
        self.text_fields["test"].setReadOnly(True)
        self.text_fields["tags"].setReadOnly(True)

        text_layout.addLayout(self.stacked_layout)
        main_layout.addLayout(text_layout, stretch=1)

        container = QWidget()
        container.setLayout(main_layout)
        self.setCentralWidget(container)

        # Set dropdown to none selected initially
        self.prompt_name_dropdown.setCurrentIndex(-1)

    # ---------------- STACKED LAYOUT -----------------

    def set_stack(self, name):
        """Set the current stack index based on the prompt name."""
        if not hasattr(self, 'stacked_layout') or self.stacked_layout is None:
            if DEBUG: print("DEBUG: stacked_layout not initialized")
            return

        if name is None:
            if DEBUG: print(f"DEBUG: Setting stack to dummy (hidden)")
            self.stacked_layout.setCurrentIndex(0)  # Show dummy widget
            return

        if name in self.text_fields:
            if DEBUG: print(f"DEBUG: Setting stack to {name}")
            # +1 because dummy widget is at index 0
            self.stacked_layout.setCurrentIndex(list(self.text_fields.keys()).index(name) + 1)
        else:
            if DEBUG: print(f"DEBUG: {name} not found in text_fields stack")

    # --------------- PROMPT CONTENT -------------------

    def get_prompt_text(self, key):

        # Check if we have a project folder set
        if not self.project_folder:
            self.text_fields["test"].setPlainText("ERROR: No project folder set")
            return ""
        
         # is this key in the prompt_file_map?
        if key not in self.prompt_file_map:
            if DEBUG: print(f"DEBUG: Prompt file not found for {key}")
            return ""
        
        prompt_file = self.prompt_file_map[key]
                
        # Parse the system prompt (function now handles prompts directory internally)
        parsed_prompt = parse_system_prompt_files(self.project_folder,
                                                  prompt_file,
                                                  getattr(self, 'current_metadata', self.metadata),
                                                  self.row_data,
                                                  self.subtitles_window
        )

        return parsed_prompt

    # -------------- DROP-DOWN CHANGES -----------------

    def define_button_states(self):

        # action type
        action_index = self.prompt_action_dropdown.currentIndex()
        if action_index != -1:
            self.action = self.prompt_action_dropdown.itemText(action_index).lower()
        else:
            self.action = None

        # source
        source_index = self.prompt_sources_dropdown.currentIndex()
        if source_index != -1:
            self.source = self.prompt_sources_dropdown.itemText(source_index).lower()
        else:
            self.source = None

        # list_type
        list_type_index = self.prompt_list_type_dropdown.currentIndex()
        if list_type_index != -1:
            self.list_type = self.prompt_list_type_dropdown.itemText(list_type_index).lower()
        else:
            self.list_type = None

    def handle_prompt_action_changed(self, idx):
        """Handle prompt action change from dropdown."""
        # if DEBUG: print(f"DEBUG: Prompt action changed at index {idx}")

        action = self.prompt_action_dropdown.itemText(idx).lower()
        if action == "tags":
            self.prompt_sources_dropdown.setCurrentIndex(-1)
            self.prompt_sources_dropdown.setEnabled(False)
            self.prompt_list_type_dropdown.setCurrentIndex(-1)
            self.prompt_list_type_dropdown.setEnabled(False)
            self.prompt_name_dropdown.setCurrentIndex(-1)
            self.prompt_name_dropdown.setEnabled(False)
            self.prompt_name_dropdown.setCurrentIndex(-1)
            self.handle_prompt_action_tags();
        elif action == "test":
            # we need to clear the stack
            self.set_stack(None)
            # we need to select "Play" or "Movie"
            self.prompt_sources_dropdown.setCurrentIndex(-1)
            self.prompt_sources_dropdown.setEnabled(True)
            # everything else is greyed out
            self.prompt_list_type_dropdown.setCurrentIndex(-1)
            self.prompt_list_type_dropdown.setEnabled(False)
            self.prompt_name_dropdown.setCurrentIndex(-1)
            self.prompt_name_dropdown.setEnabled(False)
        elif action == "prompt":
            # we need to clear the stack
            self.set_stack(None)
            self.prompt_sources_dropdown.setCurrentIndex(-1)
            self.prompt_sources_dropdown.setEnabled(True)
            self.prompt_list_type_dropdown.setCurrentIndex(-1)
            self.prompt_list_type_dropdown.setEnabled(False)
            self.prompt_name_dropdown.setCurrentIndex(-1)
            self.prompt_name_dropdown.setEnabled(False)
            # Optionally set to first prompt field
            # if self.prompt_name_dropdown.currentIndex() == -1:
            #     self.prompt_name_dropdown.setCurrentIndex(0)
        else:
            # Default: only action enabled
            self.prompt_sources_dropdown.setEnabled(False)
            self.prompt_name_dropdown.setEnabled(False)
            self.prompt_name_dropdown.setCurrentIndex(-1)

    def handle_prompt_source_changed(self, idx):
        """Handle prompt source change from dropdown."""
        source = self.prompt_sources_dropdown.itemText(idx).lower()

        # Clear the stack when source changes
        self.set_stack(None)

        # first get the action type
        action_index = self.prompt_action_dropdown.currentIndex()
        action = self.prompt_action_dropdown.itemText(action_index).lower()

        if action == "test":
            self.prompt_list_type_dropdown.setEnabled(True)
            self.prompt_list_type_dropdown.setCurrentIndex(-1)
        elif action == "prompt":
            self.prompt_name_dropdown.setEnabled(True)
            self.prompt_name_dropdown.setCurrentIndex(-1)
        else:
            pass

        if DEBUG: print(f"DEBUG: Prompt source changed at index {idx} to {source}")

    def handle_prompt_list_type_changed(self, idx):
        """Handle prompt list type change from dropdown."""

        # first get the action type
        action_index = self.prompt_action_dropdown.currentIndex()
        self.action = self.prompt_action_dropdown.itemText(action_index).lower()

        if self.action == "test":
            self.handle_test_button()
        else:
            pass

    # -------------- DROP-DOWN ACTIONS -----------------

    def handle_prompt_action_tags(self):
        """Handle prompt action for tags."""
        self.set_stack("tags")
        # Load the tags cheatsheet when switching to Tags view
        self.load_tags()

    def handle_test_button(self):
        """Test the current system prompt parsing."""

        self.define_button_states()

        if self.source is None or self.list_type is None:
            return

        # there is only one experimental file
        if (self.list_type.lower() == "experiment"):
            prompt_key = f"{self.list_type}"
        else:
            prompt_key = f"{self.source}-{self.list_type}"

        # is this key in the prompt_file_map?
        if prompt_key not in self.prompt_file_map:
            if DEBUG: print(f"DEBUG: Prompt file not found for {prompt_key}")
            return
        
        # Clear previous test output
        self.text_fields["test"].clear()

        # Set the top of the stacked layout (i.e. show) the test text_field
        self.set_stack("test")

        # Ok, Parse the prompt
        parsed_prompt = self.get_prompt_text(prompt_key)

        if parsed_prompt == "":
            if DEBUG: print(f"DEBUG: No prompt text found for {prompt_key}")
            self.text_fields["test"].setPlainText(f"ERROR: Prompt text not found for {prompt_key}")
            return

        self.text_fields["test"].setPlainText(parsed_prompt)

    def load_tags(self):
        """Load the cheatsheet for prompt tags."""
        if not self.project_folder:
            self.text_fields["tags"].setPlainText("ERROR: No project folder set")
            return
        
        prompts_dir = os.path.join(self.project_folder, "prompts")
        tags_path = os.path.join(prompts_dir, "tags.txt")

        if not os.path.exists(tags_path):
            self.text_fields["tags"].setPlainText("ERROR: Tags file not found")
            return
        
        try:
            with open(tags_path, 'r', encoding='utf-8') as f:
                tags_content = f.read()
            self.text_fields["tags"].setPlainText(tags_content)
        except Exception as e:
            self.text_fields["tags"].setPlainText(f"ERROR: Could not read tags: {e}")

    # ----------------------------------------------

    def handle_prompt_name_activated(self, idx):
        """Handle prompt name activation (including re-selection of same item)."""
        if DEBUG: print(f"DEBUG: Prompt name activated at index {idx}")

        # Handle unselected case
        if idx == -1:
            self.prompt_name = None
            if DEBUG: print(f"DEBUG: Prompt name changed to unselected")
            return

        # Update prompt name based on selection
        self.prompt_name = self.prompt_name_dropdown.itemText(idx)
        if DEBUG: print(f"DEBUG: Prompt name changed to {self.prompt_name}")

        # figure out the name of the prompt based on menu selection
        self.define_button_states()

        # if one of these names is selected, we need to append the key
        if self.prompt_name.lower() in ["system", "shot", "scene"]:
            prompt_key = f"{self.source}-{self.prompt_name.lower()}"
        else:
            prompt_key = self.prompt_name.lower()
            
        # now find the filename related to that key
        if prompt_key in self.prompt_file_map:
            filename = self.prompt_file_map[prompt_key]
        else:
            if DEBUG: print(f"DEBUG: No filename found for prompt key: {prompt_key}")
            return
        
        if DEBUG: print(f"DEBUG: Loading prompt for {prompt_key} with filename {filename}")

        # Load the prompt for the selected type
        self.load_prompt(prompt_key, filename)

    def load_prompt(self, key, filename):
        """Load the appropriate prompt file based on current prompt type."""

        # Set the index of the stacked layout to match the selected prompt type
        self.set_stack(key)

        # make sure there is a text field for this key
        if key not in self.text_fields:
            if DEBUG: print(f"DEBUG: No text field found for key: {key}")
            return

        # create a filepath based on filename
        file_path = os.path.join(self.prompts_directory, filename)
        
        # Ok, do the loading thing
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            self.text_fields[key].setPlainText(content)
            if DEBUG: print(f"DEBUG: Loaded {filename}")
        except Exception as e:
            if DEBUG: print(f"DEBUG: Could not load {filename}: {e}")
            self.text_fields[key].setPlainText(f"ERROR: Could not load {filename}")

    # ----------------------------------------------

    def handle_row_data(self, row_data):
        """Handle row data emitted from shotlist."""
        self.row_data = row_data or {}
        if DEBUG: print(f"DEBUG Prompt: Received row data: {len(self.row_data)} items")

    def handle_row_did_change(self, row_data):
        """Handle row data change emitted from shotlist."""
        # if the prompt type is test, re-run the test with new row data
        if self.action == "test":
            if DEBUG: print("DEBUG Prompt: Row data changed, re-running test")
            self.handle_test_button()

    def on_movie_loaded_with_metadata(self, movie_path, metadata):
        if DEBUG: print(f"DEBUG Prompt: on_movie_loaded called with {movie_path}")
        self.current_metadata = metadata
        
        # If test is currently selected, re-run the test with new metadata
        if self.action == "test":
            if DEBUG: print("DEBUG Prompt: test is selected, re-running test with new metadata")
            self.handle_test_button()

    def save_current_prompt(self, field_name):
        """Save the current prompt field to its corresponding file."""

        if not self.project_folder or field_name in ["test", "tags"]:
            return  # Don't save test and tags fields

        if field_name not in self.prompt_file_map:
            return

        if DEBUG: print(f"DEBUG: save_current_prompt called for {field_name}")

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
            'play-system.txt',
            'play-shot.txt',
            'play-scene.txt',
            'movie-system.txt',
            'movie-shot.txt',
            'movie-scene.txt',
            'context.txt',
            'experiment.txt',
            'format.txt',
            'goal.txt',
            'image.txt',
            'tags.txt',
            'warning.txt'
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

def parse_system_prompt_files(project_directory, filename, metadata=None, row_data=None, subtitles_window=None):
    """
    Parse a prompt file by processing the given filename and recursively resolving file tags.

    Args:
        project_directory: Path to project directory (will look for prompts/ subdirectory)
        filename: Name of the prompt file to parse (e.g., "play-system.txt")
        metadata: Movie metadata dict
        row_data: Current shot data
        subtitles_window: Reference to subtitles window

    Returns:
        str: Parsed prompt or empty string if recursion detected or file missing
    """
    if metadata is None:
        metadata = {}
    if row_data is None:
        row_data = {}

    prompts_directory = os.path.join(project_directory, "prompts")

    if not os.path.exists(prompts_directory):
        print(f"ERROR: Prompts directory not found at {prompts_directory}")
        return ""

    def read_file_safe(fname):
        try:
            filepath = os.path.join(prompts_directory, fname)
            with open(filepath, 'r', encoding='utf-8') as f:
                return f.read().strip()
        except Exception as e:
            if DEBUG: print(f"DEBUG: Could not read {fname}: {e}")
            return ""

    def parse_recursive(text, calling_stack=None):
        if calling_stack is None:
            calling_stack = []

        def replace_tag(match):
            tag = match.group(1)

            if tag.endswith('-prompt'):
                base_name = tag[:-7]
                fname = f"{base_name}.txt"
                if fname in calling_stack:
                    print(f"ERROR: Recursion detected in prompt parsing!")
                    print(f"Calling stack: {' -> '.join(calling_stack)} -> {fname}")
                    return ""
                file_content = read_file_safe(fname)
                if file_content:
                    new_stack = calling_stack + [fname]
                    parsed_content = parse_recursive(file_content, new_stack)
                    if parsed_content == "" and file_content != "":
                        return ""
                    return parsed_content
                else:
                    return f"[FILE NOT FOUND: {fname}]"

            elif tag in metadata:
                return str(metadata[tag])

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
                return "[SCENE SUBTITLES NOT YET IMPLEMENTED]"

            elif tag == "movie-subtitles":
                if subtitles_window:
                    try:
                        return subtitles_window.get_all_subtitles()
                    except Exception as e:
                        if DEBUG: print(f"DEBUG: Error getting all movie subtitles: {e}")
                        return "[ERROR RETRIEVING MOVIE SUBTITLES]"
                return "[NO SUBTITLES WINDOW AVAILABLE]"

            elif tag == "image-count":
                return str(metadata.get('image-count', '0'))

            elif tag.endswith('.txt'):
                fname = tag
                if fname in calling_stack:
                    print(f"ERROR: Recursion detected in prompt parsing!")
                    print(f"Calling stack: {' -> '.join(calling_stack)} -> {fname}")
                    return ""
                file_content = read_file_safe(fname)
                if file_content:
                    new_stack = calling_stack + [fname]
                    parsed_content = parse_recursive(file_content, new_stack)
                    if parsed_content == "" and file_content != "":
                        return ""
                    return parsed_content
                else:
                    if DEBUG: print(f"DEBUG: File {fname} not found in prompts directory")
                    return f"[FILE NOT FOUND: {fname}]"

            else:
                if DEBUG: print(f"DEBUG: Unknown tag: {tag}")
                return "{" + tag + "}"

        result = re.sub(r"\{([a-zA-Z0-9_.-]+)\}", replace_tag, text)
        return result

    file_content = read_file_safe(filename)
    if not file_content:
        print(f"ERROR: {filename} not found or empty")
        print("Prompts directory:", prompts_directory)
        return ""

    try:
        parsed_result = parse_recursive(file_content, [filename])
        if parsed_result == "" and file_content != "":
            print("ERROR: Parsing failed due to recursion or other error")
            return ""
        return parsed_result
    except Exception as e:
        print(f"ERROR: Exception during prompt parsing: {e}")
        return ""