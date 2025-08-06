DEBUG = False  # Set to True to enable debug output

from PyQt5.QtCore import QObject, pyqtSignal, Qt
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QPushButton, QLabel, QMessageBox, QFileDialog
)
from PyQt5.QtGui import QPixmap, QFont
import os

class ProjectManager(QObject):
    """Core project management logic (non-UI)"""
    
    # Signals
    project_loaded = pyqtSignal(str)  # Emitted when project folder is set/loaded
    project_changed = pyqtSignal(str)  # Emitted when project folder changes
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_folder = None
        self.parent_widget = parent  # For showing message boxes
    
    def set_project_folder(self, folder):
        """Set the project folder and validate its structure"""
        if DEBUG: 
            print(f"DEBUG: ProjectManager: Setting project folder to {folder}")
        
        # Don't reload if it's the same folder
        if self.project_folder == folder:
            if DEBUG: 
                print(f"DEBUG: ProjectManager: Project folder already set to this folder, skipping reload")
            self.project_loaded.emit(folder)
            return True
        
        # Validate and setup project structure
        if self.validate_and_setup_project_structure(folder):
            old_folder = self.project_folder
            self.project_folder = folder
            
            # Emit signals
            if old_folder != folder:
                self.project_changed.emit(folder)
            self.project_loaded.emit(folder)
            
            return True
        return False
    
    def get_project_folder(self):
        """Get the current project folder"""
        return self.project_folder
    
    def validate_and_setup_project_structure(self, folder):
        """Validate project folder structure and create missing components"""
        if DEBUG: 
            print(f"DEBUG: ProjectManager: Validating project structure in {folder}")
        
        try:
            # Get required structure
            required_folders = self.get_required_folders()
            gitignore_folders = self.get_gitignore_folders()
            required_files = self.get_required_files()
            
            # Check and create missing folders
            if not self._create_missing_folders(folder, required_folders, gitignore_folders):
                return False
            
            # Check and create missing files
            if not self._create_missing_files(folder, required_files):
                return False
            
            if DEBUG: 
                print("DEBUG: ProjectManager: All required folders and files are present.")
            return True
            
        except Exception as e:
            self._show_error(f"Failed to validate project structure:\n{str(e)}")
            return False
    
    def _create_missing_folders(self, base_folder, required_folders, gitignore_folders):
        """Create missing folders and prompt user if necessary"""
        missing_folders = []
        for required_folder in required_folders:
            folder_path = os.path.join(base_folder, required_folder)
            if not os.path.exists(folder_path):
                missing_folders.append(required_folder)
        
        if not missing_folders:
            return True
        
        # Prompt user to create missing folders
        message = ("The following project folder(s) could not be found:\n" + 
                  "\n".join(missing_folders) + 
                  "\n\nDo you want to create them now?")
        
        if not self._show_question("Missing Folders", message):
            return False
        
        # Create the folders
        try:
            for folder_name in missing_folders:
                folder_path = os.path.join(base_folder, folder_name)
                os.makedirs(folder_path, exist_ok=True)
                
                # Add .gitignore if needed
                if folder_name in gitignore_folders:
                    self._create_gitignore(folder_path)
            return True
            
        except Exception as e:
            self._show_error(f"Failed to create folders:\n{str(e)}")
            return False
    
    def _create_missing_files(self, base_folder, required_files):
        """Create missing required files"""
        for required_file in required_files:
            file_path = os.path.join(base_folder, required_file)
            if not os.path.exists(file_path):
                try:
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, 'w', encoding="utf-8") as f:
                        f.write("")  # Create an empty file
                except Exception as e:
                    self._show_error(f"Failed to create {required_file}:\n{str(e)}")
                    return False
        return True
    
    def _create_gitignore(self, folder_path):
        """Create a .gitignore file in the specified folder"""
        gitignore_path = os.path.join(folder_path, ".gitignore")
        if not os.path.exists(gitignore_path):
            try:
                with open(gitignore_path, 'w', encoding="utf-8") as f:
                    f.write("# Ignore all files in this folder (Remove this to reactivate git syncing)\n*\n")
            except Exception as e:
                self._show_error(f"Failed to create .gitignore:\n{str(e)}")
    
    def get_required_folders(self):
        """Get list of required folders for the project"""
        return [
            "datasets", 
            "gameplay", 
            "metadata", 
            "movies", 
            "posters", 
            "preferences", 
            "prompts", 
            "shotlists", 
            "subtitles",
            "thumbnails"
        ]
    
    def get_gitignore_folders(self):
        """Get list of folders that should have .gitignore files"""
        return [
            "movies", 
            "gameplay", 
            "posters", 
            "preferences", 
            "subtitles",
            "thumbnails"
        ]
    
    def get_required_files(self):
        """Get list of required files for the project"""
        return [
            "preferences/openai_api_key.txt",
            "preferences/tmdb_api_key.txt", 
            "preferences/opensubtitles_api_key.txt"
        ]
    
    def get_folder_path(self, folder_name):
        """Get the full path to a specific folder within the project"""
        if not self.project_folder:
            return None
        return os.path.join(self.project_folder, folder_name)
    
    def get_file_path(self, file_path):
        """Get the full path to a specific file within the project"""
        if not self.project_folder:
            return None
        return os.path.join(self.project_folder, file_path)
    
    def folder_exists(self, folder_name):
        """Check if a specific folder exists in the project"""
        folder_path = self.get_folder_path(folder_name)
        return folder_path and os.path.exists(folder_path)
    
    def file_exists(self, file_path):
        """Check if a specific file exists in the project"""
        full_path = self.get_file_path(file_path)
        return full_path and os.path.exists(full_path)
    
    def _show_error(self, message):
        """Show error message to user"""
        if self.parent_widget:
            QMessageBox.critical(self.parent_widget, "Project Error", message)
        else:
            print(f"PROJECT ERROR: {message}")
    
    def _show_question(self, title, message):
        """Show question dialog to user"""
        if self.parent_widget:
            msg = QMessageBox()
            msg.setWindowTitle(title)
            msg.setText(message)
            msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            msg.setDefaultButton(QMessageBox.No)

            # Set custom icon
            icon = QPixmap("ui/icons/cowgirl_folder.png")
            icon_size = 128
            scaled_icon = icon.scaled(icon_size, icon_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            msg.setIconPixmap(scaled_icon)

            result = msg.exec_()
            return result == QMessageBox.Yes
        else:
            print(f"PROJECT QUESTION: {title} - {message}")
            return True  # Default to yes in non-GUI mode
    
    # ---- Save/Load Preferences ----
    
    def get_preferences_data(self):
        """Get project data for saving to preferences"""
        return {
            "project_folder": self.project_folder
        }
    
    def load_preferences_data(self, data):
        """Load project data from preferences"""
        if "project_folder" in data and data["project_folder"]:
            folder = data["project_folder"]
            if os.path.exists(folder):
                if self.project_folder != folder:
                    self.set_project_folder(folder)
            else:
                # Project folder no longer exists, reset
                self.project_folder = None
                self.project_changed.emit("")

class ProjectWindow(QMainWindow):
    """Project management window"""
    
    # Define signals for communication
    request_save = pyqtSignal()
    request_load = pyqtSignal(dict)
    
    def __init__(self, ui):
        super().__init__()
        self.ui = ui
        
        # Create the project manager
        self.project_manager = ProjectManager(parent=self)
        
        # Track metadata rebuilding state
        self.metadata_rebuilding = False
        
        self.setup_ui()
        self.setup_connections()
    
    def setup_ui(self):
        """Setup the project window UI"""
        # Create main widget and layout
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(0,0,0,0)
        
        # Current project folder display
        self.project_folder_label = QLabel("No project folder selected")
        self.project_folder_label.setFont(self.ui.get_font('tiny-condensed'))
        self.project_folder_label.setWordWrap(True)
        self.project_folder_label.setAlignment(Qt.AlignCenter)
        self.project_folder_label.setStyleSheet("border: none;")
        layout.addWidget(self.project_folder_label)
        
        # Button layout
        button_layout = QHBoxLayout()
        button_width, button_height = self.ui.get_dimensions('button')
        
        # Select project folder button
        button_width, button_height = self.ui.get_dimensions('button')
        self.select_button = QPushButton("Project Folder")
        self.select_button.setFont(self.ui.get_font('button'))
        self.select_button.clicked.connect(self.select_project_folder)
        self.select_button.setFixedSize(120, button_height)
        button_layout.addWidget(self.select_button)
        
        layout.addLayout(button_layout)
        layout.addStretch()
        
        main_widget.setLayout(layout)
    
    def setup_connections(self):
        """Setup signal connections"""
        self.request_save.connect(self.on_request_save)
        self.request_load.connect(self.on_request_load)
        
        # Connect to project manager signals
        self.project_manager.project_loaded.connect(self.on_project_loaded)
        self.project_manager.project_changed.connect(self.on_project_changed)
    
    def select_project_folder(self):
        """Open folder dialog and set project folder"""
        folder = QFileDialog.getExistingDirectory(self, "Select Project Folder")
        if folder:  # User didn't cancel
            self.project_manager.set_project_folder(folder)
    
    def on_project_loaded(self, project_folder):
        """Handle when project is loaded"""
        if DEBUG: 
            print(f"DEBUG: ProjectWindow: Project loaded: {project_folder}")
        
        self.project_folder_label.setText(f"{project_folder}")
    
    def on_project_changed(self, project_folder):
        """Handle when project folder changes"""
        if DEBUG: 
            print(f"DEBUG: ProjectWindow: Project changed: {project_folder}")
        
        if not project_folder:
            self.project_folder_label.setText("No project folder selected")
    
    # ---- Metadata Rebuild Handlers ----
    
    def on_metadata_rebuilding_started(self):
        """Handle when metadata rebuilding starts across any catalog"""
        if DEBUG: print("DEBUG: ProjectWindow: Metadata rebuilding started - disabling project button")
        
        self.metadata_rebuilding = True
        self.select_button.setEnabled(False)
    
    def on_metadata_rebuilding_stopped(self):
        """Handle when metadata rebuilding stops across all catalogs"""
        if DEBUG: print("DEBUG: ProjectWindow: Metadata rebuilding stopped - enabling project button")
        
        self.metadata_rebuilding = False
        self.select_button.setEnabled(True)
    
    # ---- Project Manager Access ----
    
    @property 
    def project_loaded(self):
        """Expose project_loaded signal"""
        return self.project_manager.project_loaded
    
    @property
    def project_changed(self):
        """Expose project_changed signal"""
        return self.project_manager.project_changed
    
    def get_project_folder(self):
        """Get current project folder"""
        return self.project_manager.get_project_folder()
    
    def get_folder_path(self, folder_name):
        """Get folder path within project"""
        return self.project_manager.get_folder_path(folder_name)
    
    def get_file_path(self, file_path):
        """Get file path within project"""
        return self.project_manager.get_file_path(file_path)
    
    def folder_exists(self, folder_name):
        """Check if folder exists in project"""
        return self.project_manager.folder_exists(folder_name)
    
    def file_exists(self, file_path):
        """Check if file exists in project"""
        return self.project_manager.file_exists(file_path)
    
    def get_required_files(self):
        """Get required files list"""
        return self.project_manager.get_required_files()
    
    # ---- Save/Load Preferences ----
    
    def on_request_save(self):
        """Save preferences"""
        self._pending_save_data = self.project_manager.get_preferences_data()
    
    def on_request_load(self, data):
        """Load preferences"""
        self.project_manager.load_preferences_data(data)
    
    def clear_project(self):
        """Clear project - for consistency with other windows"""
        # Project window doesn't need to clear anything since it manages the project state
        # But we should reset metadata rebuilding state
        if DEBUG: print("DEBUG: ProjectWindow: clear_project called")
        
        self.metadata_rebuilding = False
        self.select_button.setEnabled(True)